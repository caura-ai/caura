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
#
# ``trust_level`` (#1202) is the one exception, and it earns it by being
# guarded rather than by being safe: it resolves ONLY when a gateway secret is
# configured and presented, which is exactly the case where the ids are the
# gateway's assertion and not the caller's. Everywhere else it degrades to
# ``None`` and nothing is looked up. See ``_trust_fields`` and the
# trust_level/trust_source tests at the bottom of this file.


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
    someone adds an UNGUARDED lookup here, this stops being true and the
    endpoint becomes a cross-tenant probe.

    Still true after ``trust_level`` (#1202) landed, and deliberately so: this
    test runs with no gateway secret configured, which is precisely the case
    where ``_trust_fields`` declines to look anything up. The identity fields
    are still pure echo. ``test_whoami_never_looks_up_without_a_configured_
    perimeter`` below asserts the same boundary from the other direction, by
    making the lookup raise if it is ever reached.
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


# --- trust_level / trust_source ---------------------------------------------
#
# A caller could not previously discover whether it was permitted to delete,
# so the served skills told agents to find out by ATTEMPTING the operation.
# On an irreversible verb that is the wrong instruction, and it is only
# defensible while the answer is undiscoverable. These fields make it
# discoverable.
#
# The two regimes genuinely differ (caura#1259): an AGENT credential is
# governed by the trust ladder, a TENANT key holds no trust level and is
# authorized by tenant scope. ``trust_source`` is what keeps
# ``trust_level: null`` from being ambiguous between the two — and from being
# confused with "the lookup did not run", which is a third, different state.
#
# ``trust_level`` is the endpoint's ONLY non-echo field. Every other value is
# reported back from what the caller sent, which is what makes an endpoint
# with no auth dependency survivable; this one is a storage lookup keyed on
# caller-supplied ids. Hence the guard, and hence these tests.


async def test_whoami_trust_level_resolved_behind_a_real_perimeter(client, monkeypatch):
    """With a gateway secret configured AND presented, the lookup runs."""
    from core_api.config import settings
    from core_api.routes import health as health_route

    async def fake_lookup(tenant_id, agent_id):
        assert (tenant_id, agent_id) == ("t-real", "a-real")
        return {"agent_id": agent_id, "trust_level": 3, "fleet_id": "f1"}

    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(health_route, "lookup_agent", fake_lookup)
    resp = await client.get(
        "/api/v1/whoami",
        headers={
            "X-Tenant-ID": "t-real",
            "X-Agent-ID": "a-real",
            "X-Gateway-Secret": "s3cret",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trust_level"] == 3
    assert data["trust_source"] == "lookup"


def _recording_lookup(monkeypatch):
    """Patch ``lookup_agent`` to RECORD calls, and return the record.

    Deliberately not a raising stub. ``_trust_fields`` wraps its lookup in
    ``except Exception`` so a probe never takes the endpoint down — which also
    means a raising stub is SWALLOWED and converted into
    ``trust_source: "unavailable"``: exactly the value a "must not look up"
    test asserts. Such a test passes whether or not the lookup ran, and every
    one of these tests did so until this was caught. Recording the call is the
    only form that survives the error handler.
    """
    from core_api.routes import health as health_route

    calls: list[tuple] = []

    async def spy(tenant_id, agent_id):
        calls.append((tenant_id, agent_id))
        return {"agent_id": agent_id, "trust_level": 3, "fleet_id": "f1"}

    monkeypatch.setattr(health_route, "lookup_agent", spy)
    return calls


async def test_whoami_never_looks_up_without_a_configured_perimeter(
    client, monkeypatch
):
    """THE SECURITY TEST. Fails without the guard in ``_trust_fields``.

    With no gateway secret configured, ``_gateway_verified`` trusts the header
    path by design, and this route has no auth dependency. An unguarded lookup
    would therefore let anyone reach any agent's trust level in any tenant by
    simply asserting the headers.

    The assertion is that the lookup DID NOT RUN, not that the response looks
    a certain way — see ``_recording_lookup`` for why the response alone
    cannot distinguish the two.
    """
    from core_api.config import settings

    calls = _recording_lookup(monkeypatch)
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    resp = await client.get(
        "/api/v1/whoami",
        headers={
            "X-Tenant-ID": "someone-elses-tenant",
            "X-Agent-ID": "someone-elses-agent",
        },
    )
    assert resp.status_code == 200, resp.text
    assert calls == [], (
        f"cross-tenant probe: looked up {calls} with no gateway secret configured"
    )
    data = resp.json()
    assert data["trust_level"] is None
    assert data["trust_source"] == "unavailable"


async def test_whoami_does_not_look_up_on_an_unverified_gateway_claim(
    client, monkeypatch
):
    """A secret IS configured but the caller did not present it.

    The identity headers are already discarded on this path, so there is no
    id to resolve — but pin that the lookup does not run, because the ids are
    still sitting in the request.
    """
    from core_api.config import settings

    calls = _recording_lookup(monkeypatch)
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "t", "X-Agent-ID": "a"},  # no X-Gateway-Secret
    )
    assert resp.status_code == 200, resp.text
    assert calls == [], f"looked up an unverified gateway identity: {calls}"
    data = resp.json()
    assert data["trust_level"] is None
    assert data["trust_source"] == "none"


async def test_whoami_trust_source_none_for_a_tenant_key(client, monkeypatch):
    """A tenant key carries no agent identity, so no trust level APPLIES.

    ``none`` rather than ``unavailable``: this is a complete answer, not a
    failure to discover one. Post-caura#1259 a tenant key is authorized to
    delete by tenant scope and is not trust-gated, so a caller seeing this
    should not go looking for a level it will never have.
    """
    from core_api.config import settings

    calls = _recording_lookup(monkeypatch)
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "t-real", "X-Gateway-Secret": "s3cret"},
    )
    assert resp.status_code == 200, resp.text
    assert calls == [], f"looked up an agent for a tenant-scoped caller: {calls}"
    data = resp.json()
    assert data["agent_id"] is None
    assert data["trust_level"] is None
    assert data["trust_source"] == "none"


async def test_whoami_trust_source_unregistered_is_distinct(client, monkeypatch):
    """An unregistered agent is a real answer, not an unknown one.

    ``enforce_delete`` refuses an unregistered identity before comparing any
    trust level, so a caller needs to tell this apart from trust-too-low.
    """
    from core_api.config import settings
    from core_api.routes import health as health_route

    async def missing(tenant_id, agent_id):
        return None

    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(health_route, "lookup_agent", missing)
    resp = await client.get(
        "/api/v1/whoami",
        headers={
            "X-Tenant-ID": "t-real",
            "X-Agent-ID": "ghost",
            "X-Gateway-Secret": "s3cret",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trust_level"] is None
    assert data["trust_source"] == "unregistered"


async def test_whoami_survives_a_storage_failure(client, monkeypatch):
    """A storage outage must not take the probe down with it.

    ``lookup_agent`` reaches storage-api, whose client calls
    ``raise_for_status()`` and retries connection errors before giving up — so
    an unguarded await turns a 5xx or an unreachable storage into a 500 here.
    That is worst precisely when it matters: ``/whoami`` answered without any
    I/O until this field existed, and a caller reaches for it when something is
    already wrong.

    Degrading to ``unavailable`` is also the honest answer rather than a
    convenient one — it says "could not determine", which is not a statement
    about the caller's permissions. Fails without the try/except in
    ``_trust_fields`` (500, not 200).
    """
    from core_api.config import settings
    from core_api.routes import health as health_route

    async def boom(tenant_id, agent_id):
        raise RuntimeError("storage-api unreachable")

    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(health_route, "lookup_agent", boom)
    resp = await client.get(
        "/api/v1/whoami",
        headers={
            "X-Tenant-ID": "t-real",
            "X-Agent-ID": "a-real",
            "X-Gateway-Secret": "s3cret",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trust_level"] is None
    assert data["trust_source"] == "unavailable"
    # The rest of the probe must still be useful — that is the point of not
    # failing the request.
    assert data["tenant_id"] == "t-real"
    assert data["via_gateway"] is True


async def test_whoami_does_not_hang_on_a_slow_backend(client, monkeypatch):
    """A backend that is UP BUT SLOW must not hang the probe.

    The failure an error-handler alone does not cover: a stalled storage never
    raises, and the storage client's read timeout is 120s with
    retry-on-transient above it. Unbounded, `/whoami` could hang for minutes —
    worst on the endpoint a caller reaches for when something is already wrong.

    Patches the bound down so the test is fast and asserts on behaviour rather
    than on wall-clock: without the ``asyncio.wait_for`` wrapper this never
    returns and the test times out instead of passing.
    """
    import asyncio as _asyncio

    from core_api.config import settings
    from core_api.routes import health as health_route

    async def never_returns(tenant_id, agent_id):
        await _asyncio.sleep(30)
        return {"trust_level": 3}

    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    monkeypatch.setattr(health_route, "lookup_agent", never_returns)
    monkeypatch.setattr(health_route, "PROBE_TIMEOUT_SECONDS", 0.05)

    resp = await _asyncio.wait_for(
        client.get(
            "/api/v1/whoami",
            headers={
                "X-Tenant-ID": "t-real",
                "X-Agent-ID": "a-real",
                "X-Gateway-Secret": "s3cret",
            },
        ),
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trust_level"] is None
    assert data["trust_source"] == "unavailable"
    assert data["tenant_id"] == "t-real"


async def test_trust_fields_guards_itself_outside_the_verified_branch(monkeypatch):
    """The guard must not depend on WHERE ``_trust_fields`` is called from.

    Its only call site today sits inside ``whoami``'s already-verified branch,
    so the request-level check is redundant — right up until someone moves the
    call or adds a second one. This calls the helper DIRECTLY, outside any
    protecting branch, with a secret configured and a request that does not
    present it. That is the shape a careless refactor would produce, and the
    lookup must still refuse to run.

    Fails if the guard is narrowed back to ``settings.gateway_shared_secret``
    truthiness alone.
    """
    from starlette.requests import Request

    from core_api.config import settings
    from core_api.routes import health as health_route

    calls = _recording_lookup(monkeypatch)
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")

    # A bare request carrying identity headers but no X-Gateway-Secret.
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/whoami",
        "headers": [(b"x-tenant-id", b"victim"), (b"x-agent-id", b"victim-agent")],
        "query_string": b"",
    }
    out = await health_route._trust_fields(Request(scope), "victim", "victim-agent")
    assert calls == [], (
        f"lookup ran for a request that never presented the gateway secret: {calls}"
    )
    assert out == {"trust_level": None, "trust_source": "unavailable"}


async def test_whoami_trust_fields_present_on_every_branch(client):
    """Shape consistency, same contract ``key_kind`` already holds."""
    resp = await client.get("/api/v1/whoami")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "trust_level" in data
    assert "trust_source" in data

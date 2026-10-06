"""ax-0917-m-16 — an agent-scoped credential need not repeat its own name.

``X-Agent-ID`` reaches ``AuthContext.agent_id`` and the REST write path already
bound to it: ``_resolve_rest_write_agent_id`` overrides the body value with the
verified identity. What it did NOT do was let the body omit the field, so an
agent credential had to send a value on every write that the very next line
threw away — while the MCP plane, for the same credential, asked for nothing
(``_refuse_default_agent_on_gateway`` returns early once ``X-Agent-ID``
resolved). These tests pin the ergonomics on both transports and, more
importantly, pin that the fix did not move the trust boundary: the body still
cannot name anyone else, and a credential that authenticates no agent still has
to say who is writing.

``_as_agent`` mirrors ``tests/test_api_read_scope_params.py``: no auth path the
OSS suite can reach populates ``auth.agent_id`` (the admin-key and standalone
branches both discard ``X-Agent-ID``), so the dependency override reproduces
the gateway's agent-scoped shape while the route body runs for real.
"""

from __future__ import annotations

import contextlib

import pytest

from tests.conftest import uid


@contextlib.contextmanager
def _as_agent(tenant_id: str, agent_id: str | None):
    """Run the enclosed requests as an AGENT credential (``auth.agent_id`` set).

    ``agent_id=None`` gives the other shape this row is about: a tenant-scoped
    credential, which authenticates no agent and must still name one.
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        tenant_id=tenant_id, agent_id=agent_id
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


@pytest.fixture
def hosted(monkeypatch):
    """Take the suite out of standalone, which is the only reason the 422 in
    question is reachable at all — standalone fills ``DEFAULT_AGENT_ID`` and
    never asks."""
    from core_api.config import settings as app_settings

    monkeypatch.setattr(app_settings, "is_standalone", False)


# ---------------------------------------------------------------------------
# REST — the field is no longer mandatory for a credential that has an identity
# ---------------------------------------------------------------------------


async def test_agent_credential_may_omit_agent_id(client, hosted):
    tenant_id = f"test-tenant-{uid()}"
    with _as_agent(tenant_id, "bound-agent"):
        resp = await client.post(
            "/api/v1/memories",
            json={"tenant_id": tenant_id, "content": f"omitted author {uid()}"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["agent_id"] == "bound-agent"


async def test_omitting_it_writes_the_same_row_as_repeating_it(client, hosted):
    """The claim the fix rests on, asserted rather than argued: the omitted
    field resolves to exactly what sending the credential's own id resolves to.
    If these two ever diverge, the relaxation stopped being a pure default."""
    tenant_id = f"test-tenant-{uid()}"
    tag = uid()
    with _as_agent(tenant_id, "bound-agent"):
        omitted = await client.post(
            "/api/v1/memories",
            json={"tenant_id": tenant_id, "content": f"omitted {tag}"},
        )
        repeated = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "content": f"repeated {tag}",
                "agent_id": "bound-agent",
            },
        )
    assert omitted.status_code == 201, omitted.text
    assert repeated.status_code == 201, repeated.text
    assert omitted.json()["agent_id"] == repeated.json()["agent_id"] == "bound-agent"


async def test_bulk_agent_credential_may_omit_agent_id(client, hosted):
    """The bulk route carried its own copy of the same block, so it needs its
    own assertion — wiring only the single write would have left the asymmetry
    in place for exactly the callers writing the most rows."""
    tenant_id = f"test-tenant-{uid()}"
    with _as_agent(tenant_id, "bound-agent"):
        resp = await client.post(
            "/api/v1/memories/bulk",
            json={
                "tenant_id": tenant_id,
                "items": [{"content": f"bulk omitted author {uid()}"}],
            },
            headers={"X-Bulk-Attempt-Id": f"m16-{uid()}"},
        )
    assert resp.status_code in (200, 201), resp.text


# ---------------------------------------------------------------------------
# The trust boundary, unmoved
# ---------------------------------------------------------------------------


async def test_body_still_cannot_name_a_peer(client, hosted):
    """The negative case, and the reason this row is security-sensitive:
    ``X-Agent-ID`` is client-supplied, so a relaxation here must not become a
    way to write under someone else's name. The credential's identity wins over
    the body exactly as before — the row is authored by the caller, not by the
    agent it named."""
    tenant_id = f"test-tenant-{uid()}"
    with _as_agent(tenant_id, "bound-agent"):
        resp = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "content": f"impersonation attempt {uid()}",
                "agent_id": "victim-agent",
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["agent_id"] == "bound-agent"


async def test_tenant_credential_still_must_name_an_agent(client, hosted):
    """The 422 survives where it earns its keep. A credential that
    authenticates no agent has nothing to fall back to, and defaulting one
    would collapse every anonymous write onto a single shared identity."""
    tenant_id = f"test-tenant-{uid()}"
    with _as_agent(tenant_id, None):
        resp = await client.post(
            "/api/v1/memories",
            json={"tenant_id": tenant_id, "content": f"no author anywhere {uid()}"},
        )
    assert resp.status_code == 422, resp.text
    assert "agent_id" in resp.text


async def test_reserved_main_credential_still_must_name_an_agent(client, hosted):
    """A credential verified as the reserved ``main`` placeholder keeps the
    422. The ``reserved_agent_id_policy`` migration depends on the BODY naming
    a real identity for that population (``effective_write_agent_id``), so
    filling it in from the credential would attribute the write to ``main`` —
    the collapse the policy exists to unwind."""
    tenant_id = f"test-tenant-{uid()}"
    with _as_agent(tenant_id, "main"):
        resp = await client.post(
            "/api/v1/memories",
            json={"tenant_id": tenant_id, "content": f"reserved home {uid()}"},
        )
    assert resp.status_code == 422, resp.text


async def test_rollback_flag_restores_the_previous_422(client, hosted, monkeypatch):
    """``bind_write_identity_to_auth=false`` is the documented emergency
    rollback for auth-derived write identity. The fallback is gated on it, so
    throwing it puts this route back exactly where it was rather than leaving a
    half-bound state behind."""
    from core_api.config import settings as app_settings

    monkeypatch.setattr(app_settings, "bind_write_identity_to_auth", False)
    tenant_id = f"test-tenant-{uid()}"
    with _as_agent(tenant_id, "bound-agent"):
        resp = await client.post(
            "/api/v1/memories",
            json={"tenant_id": tenant_id, "content": f"rolled back {uid()}"},
        )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# MCP — the other transport, which already agreed and must keep agreeing
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_mcp_agent_credential_may_omit_agent_id(mcp_env):
    """The half of the parity claim that is not about the fix: on a
    gateway-routed request with ``X-Agent-ID`` resolved, MCP never asked the
    caller to name itself, and the write is attributed to the verified identity
    rather than to the tool parameter's ``mcp-agent`` default. Asserted here so
    "the two transports now agree" is checked rather than claimed."""
    from core_api import mcp_server
    from tests.test_mcp_write import _OutStub

    mock = mcp_env["service"]("create_memory")
    mock.return_value = _OutStub("m-m16")

    gw = mcp_server._via_gateway_var.set(True)
    agent = mcp_server._agent_id_var.set("bound-agent")
    try:
        # No agent_id kwarg — the parameter keeps its DEFAULT_AGENT_ID default.
        out = await mcp_server.caura_write(content="omitted author")
    finally:
        mcp_server._agent_id_var.reset(agent)
        mcp_server._via_gateway_var.reset(gw)

    from tests._mcp_test_helpers import parse_envelope

    assert parse_envelope(out)["id"] == "m-m16"
    assert mock.await_args.args[0].agent_id == "bound-agent"


@pytest.mark.unit
async def test_mcp_body_still_cannot_name_a_peer(mcp_env):
    """The MCP twin of the impersonation negative: the tool parameter loses to
    the verified identity there too, so the relaxation on REST did not create a
    gap the other transport lacks."""
    from core_api import mcp_server
    from tests.test_mcp_write import _OutStub

    mock = mcp_env["service"]("create_memory")
    mock.return_value = _OutStub("m-m16b")

    gw = mcp_server._via_gateway_var.set(True)
    agent = mcp_server._agent_id_var.set("bound-agent")
    try:
        await mcp_server.caura_write(
            content="impersonation attempt", agent_id="victim-agent"
        )
    finally:
        mcp_server._agent_id_var.reset(agent)
        mcp_server._via_gateway_var.reset(gw)

    assert mock.await_args.args[0].agent_id == "bound-agent"

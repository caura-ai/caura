"""Unit tests for the read-only scope gate on MCP write tools (B2)
and the ASGI ContextVars hygiene fix in ``MCPAuthMiddleware`` (#32).

The audit found ``_is_write_allowed()`` defined but never invoked, so a
credential whose ``X-Capabilities`` set excluded ``write`` could still
mutate state through every MCP write surface. These tests assert that
every write tool now refuses such credentials with a FORBIDDEN envelope
(``isError=True``).

It also covers the middleware: prior to the fix, ``_readable_tenant_ids_var``
and ``_scopes_var`` were only set when their respective request headers
were present. With shared ASGI tasks or context inheritance, a follow-on
request that omitted the headers could see the prior request's values.
The middleware now resets both vars on every request.
"""

from __future__ import annotations

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import is_error_envelope, parse_envelope

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# _check_write_scope() — direct unit tests
# ---------------------------------------------------------------------------


def test_check_write_scope_passes_for_legacy_none_scope(monkeypatch):
    """Legacy credentials minted before scopes existed surface as
    ``_get_scopes() is None`` — the gate must let them through to avoid
    breaking back-compat for keys provisioned pre-rollout."""
    monkeypatch.setattr(mcp_server, "_get_scopes", lambda: None)
    assert mcp_server._check_write_scope() is None


def test_check_write_scope_passes_with_write_capability(monkeypatch):
    monkeypatch.setattr(mcp_server, "_get_scopes", lambda: {"read", "write"})
    assert mcp_server._check_write_scope() is None


def test_check_write_scope_blocks_read_only(monkeypatch):
    monkeypatch.setattr(mcp_server, "_get_scopes", lambda: {"read"})
    result = mcp_server._check_write_scope()
    assert result is not None
    assert is_error_envelope(result)


def test_check_write_scope_blocks_empty_scope_set(monkeypatch):
    """An explicitly-empty scope set means the credential carries no
    capabilities — must be refused, not interpreted as legacy."""
    monkeypatch.setattr(mcp_server, "_get_scopes", lambda: set())
    result = mcp_server._check_write_scope()
    assert result is not None
    assert is_error_envelope(result)


# ---------------------------------------------------------------------------
# Each write tool refuses a read-only credential
# ---------------------------------------------------------------------------


def _force_read_only(monkeypatch):
    """Pin the active credential to a read-only scope set for the duration
    of the test. ``_get_scopes`` is the only seam the gate consults."""
    monkeypatch.setattr(mcp_server, "_get_scopes", lambda: {"read"})


async def test_memclaw_write_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_write(content="should never persist")
    assert is_error_envelope(out)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    # No service call should have fired.
    assert "create_memory" not in mcp_env["service_mocks"]


async def test_memclaw_write_batch_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_write(items=[{"content": "x"}, {"content": "y"}])
    assert is_error_envelope(out)
    assert "create_memories_bulk" not in mcp_env["service_mocks"]


async def test_memclaw_evolve_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_evolve(outcome="x", outcome_type="success")
    assert is_error_envelope(out)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"


async def test_memclaw_tune_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_tune(top_k=10)
    assert is_error_envelope(out)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"


async def test_memclaw_keystones_set_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_keystones_set(
        op="set", doc_id="rule-1", title="t", content="c", scope="tenant", weight="low"
    )
    assert is_error_envelope(out)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"


async def test_memclaw_manage_delete_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_manage(
        op="delete", memory_id="11111111-1111-1111-1111-111111111111"
    )
    assert is_error_envelope(out)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"


async def test_memclaw_manage_bulk_delete_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_manage(
        op="bulk_delete",
        memory_ids=["11111111-1111-1111-1111-111111111111"],
    )
    assert is_error_envelope(out)


async def test_memclaw_manage_update_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_manage(
        op="update",
        memory_id="11111111-1111-1111-1111-111111111111",
        content="new content",
    )
    assert is_error_envelope(out)


async def test_memclaw_manage_transition_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_manage(
        op="transition",
        memory_id="11111111-1111-1111-1111-111111111111",
        status="archived",
    )
    assert is_error_envelope(out)


async def test_memclaw_doc_write_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_doc(
        op="write", collection="things", doc_id="d1", data={"k": "v"}
    )
    assert is_error_envelope(out)


async def test_memclaw_doc_delete_blocked_for_read_only(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    out = await mcp_server.memclaw_doc(op="delete", collection="things", doc_id="d1")
    assert is_error_envelope(out)


# ---------------------------------------------------------------------------
# Read-only credentials are still allowed for read ops
# ---------------------------------------------------------------------------


async def test_memclaw_manage_read_does_not_invoke_write_gate(mcp_env, monkeypatch):
    """``op=read`` is a query path — the gate should never fire here.
    We assert by spying on ``_check_write_scope`` and confirming it
    was not called. The handler may still fail downstream (mocked DB
    isn't awaitable), but the gate check happens before any DB work."""
    _force_read_only(monkeypatch)
    calls: list[None] = []

    def _spy():
        calls.append(None)
        return None

    monkeypatch.setattr(mcp_server, "_check_write_scope", _spy)
    try:
        await mcp_server.memclaw_manage(
            op="read", memory_id="11111111-1111-1111-1111-111111111111"
        )
    except Exception:
        # Downstream DB mocking issues are irrelevant — we only assert
        # whether the gate fired, which is decided well before the call
        # graph reaches the repository.
        pass
    assert calls == [], "write-scope gate fired on a read op"


async def test_memclaw_doc_read_does_not_invoke_write_gate(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    calls: list[None] = []

    def _spy():
        calls.append(None)
        return None

    monkeypatch.setattr(mcp_server, "_check_write_scope", _spy)
    try:
        await mcp_server.memclaw_doc(op="read", collection="things", doc_id="d1")
    except Exception:
        pass
    assert calls == [], "write-scope gate fired on a doc-read op"


async def test_memclaw_doc_query_does_not_invoke_write_gate(mcp_env, monkeypatch):
    _force_read_only(monkeypatch)
    calls: list[None] = []

    def _spy():
        calls.append(None)
        return None

    monkeypatch.setattr(mcp_server, "_check_write_scope", _spy)
    try:
        await mcp_server.memclaw_doc(op="query", collection="things", where={"k": "v"})
    except Exception:
        pass
    assert calls == [], "write-scope gate fired on a doc-query op"


# ---------------------------------------------------------------------------
# Middleware ContextVars hygiene (#32)
# ---------------------------------------------------------------------------


async def _call_middleware(headers: list[tuple[bytes, bytes]]):
    """Invoke ``MCPAuthMiddleware`` once with a synthetic ASGI scope so
    we can observe the context-var state it leaves behind. The downstream
    app is a no-op — we only care about the side effects of the middleware
    on ``_scopes_var`` / ``_readable_tenant_ids_var``."""

    async def _noop_app(scope, receive, send):
        return None

    async def _recv():
        return {"type": "http.request", "body": b"", "more_body": False}

    sends: list[dict] = []

    async def _send(message):
        sends.append(message)

    mw = mcp_server.MCPAuthMiddleware(_noop_app)
    scope = {"type": "http", "headers": headers}
    await mw(scope, _recv, _send)


async def test_middleware_resets_scopes_when_header_absent_after_present():
    """First request sets ``X-Capabilities: read`` — gate should refuse
    writes. Second request omits the header — scopes must reset to
    ``None`` so the next caller's full-scope key is honored."""
    # Before any request: known starting state.
    mcp_server._scopes_var.set(None)
    mcp_server._readable_tenant_ids_var.set(None)

    # Request 1: read-only credential.
    await _call_middleware(
        [
            (b"x-api-key", b"some-key"),
            (b"x-tenant-id", b"tenant-A"),
            (b"x-capabilities", b"read"),
        ]
    )
    assert mcp_server._get_scopes() == {"read"}
    assert mcp_server._is_write_allowed() is False

    # Request 2: header omitted entirely — must NOT inherit "read" scope.
    await _call_middleware(
        [
            (b"x-api-key", b"some-key"),
            (b"x-tenant-id", b"tenant-B"),
        ]
    )
    assert mcp_server._get_scopes() is None
    assert mcp_server._is_write_allowed() is True


async def test_middleware_resets_readable_tenants_when_header_absent_after_present():
    mcp_server._scopes_var.set(None)
    mcp_server._readable_tenant_ids_var.set(None)

    await _call_middleware(
        [
            (b"x-api-key", b"some-key"),
            (b"x-tenant-id", b"tenant-A"),
            (b"x-readable-tenant-ids", b"tenant-A,tenant-B,tenant-C"),
        ]
    )
    assert mcp_server._get_readable_tenants() == ["tenant-A", "tenant-B", "tenant-C"]

    # Second request without the header: must reset, not inherit.
    await _call_middleware(
        [
            (b"x-api-key", b"some-key"),
            (b"x-tenant-id", b"tenant-D"),
        ]
    )
    assert mcp_server._get_readable_tenants() == []


async def test_middleware_honors_x_key_scopes_alias():
    """The X-Key-Scopes header is the back-compat alias for X-Capabilities
    during the gateway rollout. It must populate the same context var."""
    mcp_server._scopes_var.set(None)

    await _call_middleware(
        [
            (b"x-api-key", b"some-key"),
            (b"x-tenant-id", b"tenant-A"),
            (b"x-key-scopes", b"read"),
        ]
    )
    assert mcp_server._get_scopes() == {"read"}

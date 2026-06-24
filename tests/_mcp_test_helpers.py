"""Shared helpers for unit-testing MCP tool handlers in isolation.

The handlers in ``core_api.mcp_server`` depend on:
  - ``_check_auth()`` — returns ``None`` on pass
  - ``_get_tenant()`` — returns the current tenant_id
  - ``_mcp_session()`` — async context manager yielding a SQLAlchemy session
  - service-layer calls (e.g., ``create_memory``, ``search_memories``)

These helpers patch those out so tests can exercise validation, op
dispatch, error-envelope construction, and trust gating without a DB.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


_LATENCY_SUFFIX_RE = re.compile(r"\n\n_latency_ms:\s*\d+\s*$")


def strip_latency(result: Any) -> str:
    """Drop the ``_latency_ms`` trailer from a non-JSON handler response.

    Accepts both ``str`` (success-path) and ``CallToolResult`` (post-B2
    error-path) — for the latter the JSON envelope's ``_latency_ms``
    key would be the relevant marker, but tests using this helper
    today care about the text-trailer form, so we flatten via
    ``as_text`` and strip the trailer.
    """
    return _LATENCY_SUFFIX_RE.sub("", as_text(result))


def as_text(result: Any) -> str:
    """Return the textual content of a handler result regardless of
    shape — used for tests that ``assert "X" in out``. Pre-B2 (FRICTION-
    REPORT-V3) handlers returned strings for both success and error;
    post-B2 the error path returns a ``CallToolResult``. ``mcp.call_tool``
    callers may also see a bare ``Sequence[ContentBlock]`` when the handler
    returned a string that FastMCP wrapped into text content. This helper
    bridges all three so the substring-check idiom keeps working.
    """
    from mcp.types import CallToolResult, TextContent

    if isinstance(result, CallToolResult):
        first = result.content[0] if result.content else None
        return first.text if isinstance(first, TextContent) else ""
    if isinstance(result, (list, tuple)) and result:
        first = result[0]
        return first.text if isinstance(first, TextContent) else ""
    return result if isinstance(result, str) else str(result)


def parse_envelope(result: Any) -> dict[str, Any]:
    """Parse a JSON response (error envelope or payload) from a handler.

    Delegates text extraction to ``as_text`` so all three shapes —
    ``str``, ``CallToolResult``, and ``Sequence[ContentBlock]`` — are
    handled uniformly. Handlers that wrap JSON get a top-level
    ``_latency_ms`` key merged in; we strip it so tests can assert on
    the semantic payload.
    """
    data = json.loads(as_text(result))
    if isinstance(data, dict):
        data.pop("_latency_ms", None)
    return data


def is_error_envelope(result: Any) -> bool:
    """Return True iff ``result`` carries ``isError=True``.

    Mirrors the post-B2 contract: any tool returning a structured
    ``{"error": {...}}`` envelope reaches the MCP client as a
    ``CallToolResult`` with ``isError=True``. Use in tests that need
    to confirm the boolean flip, not just the JSON content.
    """
    from mcp.types import CallToolResult

    return isinstance(result, CallToolResult) and result.isError is True


def stub_storage_client(monkeypatch, **method_returns):
    """Replace ``get_storage_client`` with a MagicMock whose methods return
    the requested values. Each kwarg is the method name (e.g. ``get_agent``,
    ``list_document_collections``) and the value is the awaited result.

    Fix 2 Phase 4 routed the 9 ready MCP tools through the core-storage-api
    HTTP client, so the handlers resolve their DB access via
    ``get_storage_client().<method>`` rather than the old ``db``-bound repo /
    service calls. Unit tests that want deterministic returns / call-arg
    assertions stub the client here. (The conftest's autouse ASGI bridge would
    otherwise serve real in-process storage on the test engine — fine for
    integration coverage, but these unit tests assert on the exact storage
    interaction.) Mirrors the helper ``test_mcp_keystones`` introduced for the
    already-routed keystone tools.
    """
    sc = MagicMock(name="storage_client")
    for name, ret in method_returns.items():
        setattr(sc, name, AsyncMock(return_value=ret))

    def _factory():
        return sc

    # The handler binds ``get_storage_client`` at module import time, so the
    # test must patch the alias on ``mcp_server`` (where Python resolves it at
    # call time) — not the original module path.
    monkeypatch.setattr("core_api.mcp_server.get_storage_client", _factory)
    return sc


@pytest.fixture
def mcp_env(monkeypatch):
    """Patch the common MCP handler dependencies and yield a control dict.

    Usage::

        async def test_something(mcp_env):
            mcp_env["service"]("create_memory").return_value = ...
            out = await mcp_server.memclaw_write(content="hello", ...)
            assert ...

    The control object exposes:
      - ``service(name)`` → AsyncMock you can configure per-service-call.
        Looked up as ``core_api.services.{module}.{name}`` by matching one
        of the known import paths used by handlers.
      - ``db`` → the MagicMock stand-in for the DB session.
      - ``tenant`` → the fake tenant_id (override before patching if needed).
    """
    from core_api import mcp_server

    tenant = "test-tenant"
    db = MagicMock(name="db")

    # Make db.commit/execute awaitable (they're called via `await`).
    db.commit = AsyncMock()
    db.execute = AsyncMock()

    @contextlib.asynccontextmanager
    async def fake_session():
        yield db

    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant)
    # Fix 2 Ph5b (PR2): ``_mcp_session`` was deleted once ``memclaw_evolve`` —
    # its last consumer — migrated to ``_no_db()``. Every MCP handler now opens
    # ``_no_db()`` (yields None; storage-routed services carry tenant context
    # explicitly). Patch it with the MagicMock-yielding session so handlers that
    # still bind ``async with _no_db() as db`` get a harmless stand-in.
    monkeypatch.setattr(mcp_server, "_no_db", fake_session)

    # Stub out usage metering so it doesn't hit the DB.
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock(return_value=None))

    # `_require_trust` is exercised directly in tests that need it; here we
    # pre-emptively bypass it so handlers under test don't fail on agent lookup.
    async def _always_allow(tenant_id, agent_id, min_level):
        return 3, False, None  # max trust, not_found=False, no error

    monkeypatch.setattr(mcp_server, "_require_trust", _always_allow)

    # Write tools call ``enforce_fleet_write`` to lazy-create the Agent row;
    # in unit tests there's no real DB, so stub it as a no-op returning the
    # caller's identity. Tests that want to assert the call replace this via
    # ``service("enforce_fleet_write")``.
    async def _stub_enforce_fleet_write(tenant_id, agent_id, fleet_id):
        return {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "trust_level": 3,
        }

    monkeypatch.setattr(mcp_server, "enforce_fleet_write", _stub_enforce_fleet_write)

    service_mocks: dict[str, AsyncMock] = {}

    def service(name: str) -> AsyncMock:
        """Get or create a per-service-call AsyncMock.

        Handlers reference service functions either via module-level import
        or via inner ``from … import`` — we overwrite the mcp_server-level
        attribute where one exists, and register a name→mock lookup that
        tests can seed.
        """
        if name not in service_mocks:
            service_mocks[name] = AsyncMock(name=name)
        if hasattr(mcp_server, name):
            monkeypatch.setattr(mcp_server, name, service_mocks[name])
        return service_mocks[name]

    yield {
        "service": service,
        "db": db,
        "tenant": tenant,
        "monkeypatch": monkeypatch,
        "service_mocks": service_mocks,
    }

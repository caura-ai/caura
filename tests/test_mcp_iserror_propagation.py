"""CAURA-000 FRICTION-REPORT-V3 B2: MCP error envelopes must reach
the client as ``CallToolResult(isError=True)`` so clients doing
``if not result.isError: succeed()`` don't silently treat
FORBIDDEN / INVALID_ARGUMENTS / NOT_FOUND as success.

These tests bypass the FastMCP transport and exercise the tool
functions directly — the same surface the rest of the unit-test
suite uses. We check both:

  1. The error-shape callsites return a ``CallToolResult`` (not a
     plain string), and
  2. ``isError`` is ``True`` on that result.

If the wrapping breaks, these go red before a friction report does.
"""

from __future__ import annotations

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import is_error_envelope, parse_envelope

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_invalid_args_via_with_latency_sets_iserror(mcp_env):
    """``memclaw_write`` with neither content nor items goes through
    ``_with_latency(_error_response(...))``. The wrapper must promote
    the result to ``isError=True``."""
    out = await mcp_server.memclaw_write()
    assert is_error_envelope(out), f"expected isError=True, got {out!r}"
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_invalid_args_raw_return_sets_iserror(mcp_env):
    """``memclaw_list`` with a bad scope returns the error via a
    raw ``return _error_response(...)`` callsite (now wrapped through
    ``_with_latency`` for consistency)."""
    out = await mcp_server.memclaw_list(scope="everywhere")
    assert is_error_envelope(out)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_invalid_memory_id_sets_iserror(mcp_env):
    """``memclaw_manage`` with a malformed memory_id returns the
    INVALID_ARGUMENTS envelope through the raw-return path."""
    out = await mcp_server.memclaw_manage(op="read", memory_id="not-a-uuid")
    assert is_error_envelope(out)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "memory_id" in payload["error"]["message"]


def test_pre_baked_auth_errors_are_call_tool_results():
    """The two module-level constants returned by ``_check_auth`` are
    the carriers of the auth-failure signal — every tool that calls
    ``if err := _check_auth(): return err`` propagates one of them
    unchanged. They must already be ``CallToolResult(isError=True)``
    or the wrap silently drops at the tool boundary."""
    from mcp.types import CallToolResult

    assert isinstance(mcp_server._AUTH_ERROR, CallToolResult)
    assert mcp_server._AUTH_ERROR.isError is True
    assert parse_envelope(mcp_server._AUTH_ERROR)["error"]["code"] == "UNAUTHORIZED"

    assert isinstance(mcp_server._ADMIN_ERROR, CallToolResult)
    assert mcp_server._ADMIN_ERROR.isError is True
    assert parse_envelope(mcp_server._ADMIN_ERROR)["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_success_path_unchanged(mcp_env, monkeypatch):
    """Sanity check: success-path responses are still plain strings —
    we only flip ``isError`` for ``{"error": ...}`` envelopes. A
    success return going through ``_with_latency`` stays a JSON string
    (which FastMCP then wraps with ``isError=False``)."""
    from unittest.mock import MagicMock

    def _mock_result(rows):
        scalars = MagicMock()
        scalars.all.return_value = rows
        result = MagicMock()
        result.scalars.return_value = scalars
        return result

    mcp_env["db"].execute.return_value = _mock_result([])
    out = await mcp_server.memclaw_list()
    assert isinstance(out, str), f"success path must stay str, got {type(out).__name__}"
    payload = parse_envelope(out)
    assert "error" not in payload


# ---------------------------------------------------------------------------
# Codebase-audit 2026-05-19 finding B4 — 5 raw-string ``"Error: ..."`` returns
# in ``mcp_server.py`` previously slipped past ``_with_latency``'s JSON-shape
# detection and reached the MCP client as ``isError=False``. After the fix
# all 5 sites must produce structured envelopes with ``isError=True``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b4_manage_update_no_fields_sets_iserror(mcp_env):
    """``memclaw_manage(op='update')`` with no field args is the
    ``"Error: No fields to update..."`` site."""
    out = await mcp_server.memclaw_manage(
        op="update",
        memory_id="00000000-0000-0000-0000-000000000001",
    )
    assert is_error_envelope(out), f"expected isError=True, got {out!r}"
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "No fields to update" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_b4_doc_write_no_embedding_sets_iserror(mcp_env, monkeypatch):
    """``memclaw_doc(op='write', ...)`` when the embedding provider returns
    ``None`` is the ``"Error: embedding provider returned no vector ...
    Write aborted."`` site."""
    import common.embedding as _emb

    async def _no_vector(_text: str):
        return None

    monkeypatch.setattr(_emb, "get_embedding", _no_vector)
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="notes",
        doc_id="d1",
        data={"summary": "hello"},
    )
    assert is_error_envelope(out), f"expected isError=True, got {out!r}"
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "UPSTREAM_ERROR"
    assert "Write aborted" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_b4_doc_search_no_embedding_sets_iserror(mcp_env, monkeypatch):
    """``memclaw_doc(op='search', ...)`` when the embedding provider returns
    ``None`` is the ``"Error: embedding provider returned no vector ...
    Search aborted."`` site."""
    import common.embedding as _emb

    async def _no_vector(_text: str):
        return None

    monkeypatch.setattr(_emb, "get_embedding", _no_vector)
    out = await mcp_server.memclaw_doc(op="search", query="anything")
    assert is_error_envelope(out), f"expected isError=True, got {out!r}"
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "UPSTREAM_ERROR"
    assert "Search aborted" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_b4_insights_unregistered_agent_sets_iserror(mcp_env, monkeypatch):
    """``memclaw_insights`` with ``_require_trust`` returning ``not_found=True``
    is one of the two ``"Error (403): Agent ... is not registered"`` sites."""

    async def _not_found(tenant_id, agent_id, min_level):
        return 0, True, None

    monkeypatch.setattr(mcp_server, "_require_trust", _not_found)
    out = await mcp_server.memclaw_insights(focus="patterns", scope="agent")
    assert is_error_envelope(out), f"expected isError=True, got {out!r}"
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    assert "is not registered" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_b4_evolve_unregistered_agent_sets_iserror(mcp_env, monkeypatch):
    """``memclaw_evolve`` with ``_require_trust`` returning ``not_found=True``
    is the second ``"Error (403): Agent ... is not registered"`` site."""

    async def _not_found(tenant_id, agent_id, min_level):
        return 0, True, None

    monkeypatch.setattr(mcp_server, "_require_trust", _not_found)
    out = await mcp_server.memclaw_evolve(
        outcome="shipped a thing",
        outcome_type="success",
        scope="agent",
    )
    assert is_error_envelope(out), f"expected isError=True, got {out!r}"
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    assert "is not registered" in payload["error"]["message"]

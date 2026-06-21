"""Fix 2 Phase 4 — MCP tools surface storage HTTP errors as the canonical envelope.

Regression test for the claude-review finding on PR #432: `memclaw_recall` and
`memclaw_list` route through the storage HTTP client, whose calls raise
`httpx.HTTPStatusError` on a non-2xx. Both must catch it and return the canonical
error envelope rather than letting the exception escape to the MCP framework raw.
`memclaw_recall` was missing the `except httpx.HTTPStatusError` clause its siblings
have; `memclaw_list` had no try/except around its storage call at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope, stub_storage_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _storage_err(status: int = 503) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://storage/x")
    return httpx.HTTPStatusError("storage down", request=req, response=httpx.Response(status, request=req))


async def test_list_surfaces_storage_error_as_envelope(mcp_env, monkeypatch):
    """A storage 5xx from list_memories_by_filters → error envelope, not a raw raise."""
    sc = stub_storage_client(monkeypatch, list_memories_by_filters=[])
    sc.list_memories_by_filters.side_effect = _storage_err()
    out = await mcp_server.memclaw_list(agent_id="alice", scope="agent")
    assert "error" in parse_envelope(out)


async def test_recall_surfaces_storage_error_as_envelope(mcp_env, monkeypatch):
    """A storage 5xx from sc.get_agent → error envelope, not a raw raise."""
    monkeypatch.setattr(mcp_server, "resolve_config", AsyncMock(return_value=MagicMock()))
    sc = stub_storage_client(monkeypatch, get_agent=None)
    sc.get_agent.side_effect = _storage_err()
    out = await mcp_server.memclaw_recall(query="anything")
    assert "error" in parse_envelope(out)

"""Unit tests for ``memclaw_keystones`` and ``memclaw_keystones_set`` (CAURA-000).

Covers:
- Read: auth, payload shape, truncation flag pass-through, fleet/agent scoping.
- Write/delete: op validation, trust gate, payload pass-through, error envelopes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope, strip_latency

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_storage_client(monkeypatch, **method_returns):
    """Replace ``get_storage_client`` with a MagicMock whose methods return
    the requested values. Each kwarg is the method name (e.g. ``list_keystones``)
    and the value is the awaited result.
    """
    sc = MagicMock(name="storage_client")
    for name, ret in method_returns.items():
        setattr(sc, name, AsyncMock(return_value=ret))

    def _factory():
        return sc

    # The handler binds ``get_storage_client`` at module import time, so
    # the test must patch the alias on ``mcp_server`` (where Python
    # resolves it at call time) — not the original module path.
    monkeypatch.setattr("core_api.mcp_server.get_storage_client", _factory)
    return sc


# ---------------------------------------------------------------------------
# memclaw_keystones (read)
# ---------------------------------------------------------------------------


async def test_keystones_read_returns_rules(mcp_env, monkeypatch):
    rows = [
        {"doc_id": "no-secrets", "data": {"scope": "tenant", "weight": 100}},
        {"doc_id": "use-feature-x", "data": {"scope": "fleet", "weight": 50}},
    ]
    _stub_storage_client(monkeypatch, list_keystones=(rows, False))
    out = await mcp_server.memclaw_keystones(fleet_id="fleet-A")
    payload = parse_envelope(out)
    assert payload["count"] == 2
    assert payload["truncated"] is False
    assert [r["doc_id"] for r in payload["rules"]] == ["no-secrets", "use-feature-x"]


async def test_keystones_read_propagates_truncation(mcp_env, monkeypatch):
    _stub_storage_client(monkeypatch, list_keystones=([{"doc_id": "a"}], True))
    out = await mcp_server.memclaw_keystones(fleet_id="fleet-A")
    assert parse_envelope(out)["truncated"] is True


async def test_keystones_read_drops_agent_id_when_no_fleet(mcp_env, monkeypatch):
    """agent_id without fleet_id can't resolve agent-scope rows; the handler
    must NOT forward agent_id under that shape (would silently miss them at
    the storage layer anyway, but defence in depth)."""
    sc = _stub_storage_client(monkeypatch, list_keystones=([], False))
    await mcp_server.memclaw_keystones(agent_id="agent-Z", fleet_id=None)
    sc.list_keystones.assert_awaited_once()
    kwargs = sc.list_keystones.await_args.kwargs
    assert kwargs["agent_id"] is None
    assert kwargs["fleet_id"] is None


# ---------------------------------------------------------------------------
# memclaw_keystones_set (write/delete)
# ---------------------------------------------------------------------------


async def test_keystones_set_unknown_op(mcp_env):
    out = await mcp_server.memclaw_keystones_set(op="oops", doc_id="x")
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "set|delete" in payload["error"]["message"]


async def test_keystones_set_missing_doc_id(mcp_env):
    out = await mcp_server.memclaw_keystones_set(op="set", doc_id="")
    assert "doc_id is required" in strip_latency(out)


async def test_keystones_set_trust_denied(mcp_env, monkeypatch):
    """Low-trust agent must be rejected — keystones override user instructions,
    so a compromised agent cannot be allowed to plant one."""

    async def _deny(db, tenant_id, agent_id, min_level):
        return 0, False, "INSUFFICIENT_TRUST level 1 required"

    monkeypatch.setattr(mcp_server, "_require_trust", _deny)
    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="no-secrets",
        title="No secrets",
        content="Never commit credentials.",
        scope="tenant",
        weight="high",
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"


async def test_keystones_set_happy_path(mcp_env, monkeypatch):
    # Use a real UUID — audit_logs' resource_id validator (storage side)
    # rejects non-UUID strings with 422 and would mask the happy-path
    # assertion below.
    sc = _stub_storage_client(
        monkeypatch,
        upsert_keystone={
            "id": "11111111-1111-4111-8111-111111111111",
            "doc_id": "no-secrets",
        },
    )
    out = await mcp_server.memclaw_keystones_set(
        op="set",
        doc_id="no-secrets",
        title="No secrets",
        content="Never commit credentials.",
        scope="tenant",
        weight="high",
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["action"] == "set"
    assert payload["doc_id"] == "no-secrets"
    # Storage was invoked once with the full payload — scope/weight/etc passed through.
    sc.upsert_keystone.assert_awaited_once()
    sent = sc.upsert_keystone.await_args.args[0]
    assert sent["scope"] == "tenant"
    assert sent["weight"] == "high"
    assert sent["doc_id"] == "no-secrets"


async def test_keystones_delete_happy_path(mcp_env, monkeypatch):
    _stub_storage_client(monkeypatch, delete_keystone=True)
    out = await mcp_server.memclaw_keystones_set(op="delete", doc_id="no-secrets")
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["action"] == "delete"


async def test_keystones_delete_not_found(mcp_env, monkeypatch):
    _stub_storage_client(monkeypatch, delete_keystone=False)
    out = await mcp_server.memclaw_keystones_set(op="delete", doc_id="ghost")
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "NOT_FOUND"

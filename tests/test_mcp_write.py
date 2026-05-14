"""Unit tests for ``memclaw_write`` (single OR batch).

Covers:
- Single-write happy path.
- Batch-write happy path.
- Mutual exclusion — neither ``content`` nor ``items`` → ``INVALID_ARGUMENTS``.
- Mutual exclusion — both → ``INVALID_ARGUMENTS``.
- Batch size > 100 → ``BATCH_TOO_LARGE`` with received/max details.
- Invalid items (missing required `content`) → ``INVALID_BATCH_ITEM``.
- Service ``HTTPException`` → ``Error (…)`` envelope.
- Auth failure short-circuits.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from tests._mcp_test_helpers import as_text, parse_envelope

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _OutStub:
    """Minimal stand-in for the object returned by service create_memory/create_memories_bulk."""

    def __init__(self, mid: str = "m-1"):
        self.mid = mid

    def model_dump(self, mode: str = "python"):  # noqa: ARG002
        return {"id": self.mid, "status": "created"}


async def test_write_single_happy_path(mcp_env):
    mcp_env["service"]("create_memory").return_value = _OutStub("m-1")

    out = await mcp_server.memclaw_write(content="a fact I remembered")
    payload = parse_envelope(out)
    assert payload["id"] == "m-1"
    mcp_env["service_mocks"]["create_memory"].assert_awaited_once()


async def test_write_registers_agent(mcp_env):
    # memclaw_write must lazy-create the Agent row (REST parity). Without this,
    # an mca_-key holder's first MCP write succeeds but PATCH /agents/{id}/trust
    # 404s because no Agent row exists.
    enforce = mcp_env["service"]("enforce_fleet_write")
    enforce.return_value = {"agent_id": "a1", "trust_level": 0}
    mcp_env["service"]("create_memory").return_value = _OutStub("m-2")

    await mcp_server.memclaw_write(
        content="first write", agent_id="a1", fleet_id="f1"
    )
    enforce.assert_awaited_once()
    args = enforce.await_args.args
    # Signature: (db, tenant_id, agent_id, fleet_id)
    assert args[1] == mcp_env["tenant"]
    assert args[2] == "a1"
    assert args[3] == "f1"


async def test_write_batch_registers_agent(mcp_env):
    enforce = mcp_env["service"]("enforce_fleet_write")
    mcp_env["service"]("create_memories_bulk").return_value = _OutStub("batch-2")

    await mcp_server.memclaw_write(
        items=[{"content": "one"}, {"content": "two"}],
        agent_id="a2",
        fleet_id="f2",
    )
    enforce.assert_awaited_once()
    args = enforce.await_args.args
    assert args[2] == "a2"
    assert args[3] == "f2"


async def test_write_batch_happy_path(mcp_env):
    mcp_env["service"]("create_memories_bulk").return_value = _OutStub("batch-1")

    out = await mcp_server.memclaw_write(
        items=[{"content": "one"}, {"content": "two"}]
    )
    payload = parse_envelope(out)
    assert payload["id"] == "batch-1"
    mcp_env["service_mocks"]["create_memories_bulk"].assert_awaited_once()


async def test_write_neither_content_nor_items_errors(mcp_env):
    out = await mcp_server.memclaw_write()
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "exactly one of" in payload["error"]["message"]
    assert payload["error"]["details"]["received_content"] is False
    assert payload["error"]["details"]["received_items"] is False


async def test_write_both_content_and_items_errors(mcp_env):
    out = await mcp_server.memclaw_write(
        content="a fact", items=[{"content": "conflicting"}]
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert payload["error"]["details"]["received_content"] is True
    assert payload["error"]["details"]["received_items"] is True


async def test_write_batch_too_large(mcp_env):
    too_many = [{"content": f"m{i}"} for i in range(101)]
    out = await mcp_server.memclaw_write(items=too_many)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "BATCH_TOO_LARGE"
    assert payload["error"]["details"]["received"] == 101
    assert payload["error"]["details"]["max"] == 100


async def test_write_invalid_batch_item(mcp_env):
    """Item without ``content`` trips BulkMemoryItem validation → INVALID_BATCH_ITEM."""
    out = await mcp_server.memclaw_write(
        items=[{"content": "good"}, {"weight": 0.5}]
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_BATCH_ITEM"
    assert payload["error"]["details"]["received_count"] == 2


async def test_write_service_http_exception_becomes_envelope(mcp_env):
    # Non-duplicate 4xx still maps to the error envelope.
    mcp_env["service"]("create_memory").side_effect = HTTPException(
        status_code=409, detail="some other conflict"
    )
    out = await mcp_server.memclaw_write(content="dup")
    assert "CONFLICT" in as_text(out)
    assert "some other conflict" in as_text(out)


async def test_write_exact_duplicate_returns_idempotent_envelope(mcp_env):
    # When the dedup gate trips (Stage 5's per-agent exact-hash dedup), the
    # MCP write surface returns 200 with status=duplicate and the existing
    # memory id. Lets callers safely retry without branching on 4xx codes.
    existing_id = "11111111-2222-3333-4444-555555555555"
    mcp_env["service"]("create_memory").side_effect = HTTPException(
        status_code=409, detail=f"Duplicate memory exists: {existing_id}"
    )
    out = await mcp_server.memclaw_write(content="same content", agent_id="a1")
    payload = parse_envelope(out)
    assert payload["status"] == "duplicate"
    assert payload["existing_id"] == existing_id
    assert payload["agent_id"] == "a1"


async def test_write_near_duplicate_still_errors(mcp_env):
    # Semantic-duplicate (different prefix) is NOT idempotent — caller wrote
    # new content that the service suppressed; surface as error.
    mcp_env["service"]("create_memory").side_effect = HTTPException(
        status_code=409,
        detail="Near-duplicate memory exists: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    out = await mcp_server.memclaw_write(content="paraphrase")
    assert "CONFLICT" in as_text(out)
    assert "Near-duplicate" in as_text(out)


async def test_write_auth_failure_shortcircuits(monkeypatch):
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)
    out = await mcp_server.memclaw_write(content="hello")
    assert out == mcp_server._AUTH_ERROR


async def test_write_passes_visibility_through(mcp_env):
    """visibility flag reaches the service layer unchanged."""
    mock = mcp_env["service"]("create_memory")
    mock.return_value = _OutStub()

    await mcp_server.memclaw_write(content="shared fact", visibility="scope_org")
    kwargs = mock.await_args.kwargs
    model = kwargs.get("__self__", None) or mock.await_args.args[1]
    assert model.visibility == "scope_org"


async def test_write_passes_fleet_id_kwarg_through(mcp_env):
    """``fleet_id`` kwarg reaches the persisted ``MemoryCreate`` model.

    Regression guard for the finding
    ``memclaw_write_fleet_id_not_propagated_from_url``: the MCP URL
    query ``?fleet_id=…`` is NOT applied to memory rows on write, so
    the only path that reliably tags a memory with a fleet is the
    explicit kwarg. If someone ever refactors the handler in a way
    that drops the kwarg on the floor, this test fails loudly rather
    than silently persisting ``fleet_id=NULL``.
    """
    mock = mcp_env["service"]("create_memory")
    mock.return_value = _OutStub()

    await mcp_server.memclaw_write(
        content="memo for caura-rnd-fleet", fleet_id="caura-rnd-fleet"
    )
    kwargs = mock.await_args.kwargs
    model = kwargs.get("__self__", None) or mock.await_args.args[1]
    assert model.fleet_id == "caura-rnd-fleet"


async def test_write_refuses_default_agent_on_gateway(mcp_env):
    # Gateway-routed request + tenant key (no X-Agent-ID injection) + caller
    # left agent_id at the default → MISSING_AGENT_ID. Standalone (default
    # in tests) is unaffected.
    mcp_env["service"]("create_memory").return_value = _OutStub("m-x")
    from core_api import mcp_server

    token = mcp_server._via_gateway_var.set(True)
    try:
        out = await mcp_server.memclaw_write(content="anything")
    finally:
        mcp_server._via_gateway_var.reset(token)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "MISSING_AGENT_ID"
    # Create wasn't called because we short-circuited.
    mcp_env["service_mocks"]["create_memory"].assert_not_awaited()


async def test_write_explicit_agent_on_gateway_allowed(mcp_env):
    # Same gateway-routed path, but caller passed an explicit agent_id —
    # the guard doesn't fire and the write proceeds.
    mcp_env["service"]("create_memory").return_value = _OutStub("m-y")
    from core_api import mcp_server

    token = mcp_server._via_gateway_var.set(True)
    try:
        out = await mcp_server.memclaw_write(content="hi", agent_id="real-agent")
    finally:
        mcp_server._via_gateway_var.reset(token)
    payload = parse_envelope(out)
    assert payload["id"] == "m-y"


async def test_write_default_agent_in_standalone_ok(mcp_env):
    # Standalone (no gateway) keeps the default-identity ergonomics.
    mcp_env["service"]("create_memory").return_value = _OutStub("m-z")
    out = await mcp_server.memclaw_write(content="hi")
    payload = parse_envelope(out)
    assert payload["id"] == "m-z"


async def test_write_without_fleet_id_persists_null(mcp_env):
    """Absent ``fleet_id`` → model carries ``None``. This is the current
    server-side behavior; pairs with the skill guidance that tells
    agents to pass the kwarg explicitly (URL param alone won't do it)."""
    mock = mcp_env["service"]("create_memory")
    mock.return_value = _OutStub()

    await mcp_server.memclaw_write(content="no fleet")
    kwargs = mock.await_args.kwargs
    model = kwargs.get("__self__", None) or mock.await_args.args[1]
    assert model.fleet_id is None

"""Unit tests for ``memclaw_list`` — non-semantic memory enumeration.

Covers:
- Scope-based trust gating: scope='agent' at trust ≥ 1; scope='fleet' own-fleet at
  trust ≥ 1, cross-fleet at trust ≥ 2; scope='all' at trust ≥ 2.
- scope='agent' forces written_by to the caller's agent_id.
- Filter / sort / order validation (422).
- ``include_deleted`` only honored at trust ≥ 3 (silently ignored below).
- Invalid cursor / ISO dates.
- Cursor vs sort/order constraint (only created_at/desc).
- Happy path (zero rows) shape: ``{count, results, next_cursor, scope}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import as_text, parse_envelope, stub_storage_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# Fix 2 Phase 4: ``memclaw_list`` ports ``memory_repo.list_by_filters`` into the
# storage layer (``PostgresService.memory_list_by_filters``) and calls it via
# ``sc.list_memories_by_filters(payload)``. The visibility / cursor / deleted_at
# SQL now lives in core-storage-api (covered by its own service tests against the
# real test DB); the core-api unit tests here assert the PAYLOAD the tool sends
# (scope→written_by, include_deleted gating, limit clamp) and the response
# shaping (slice + next_cursor) over stubbed storage rows.


def _out_stub(mid: str):
    class _Out:
        def model_dump(self, mode="python"):  # noqa: ARG002
            return {"id": mid, "content": f"memory {mid}"}

    return _Out()


async def test_list_scope_agent_allowed_at_trust_1(mcp_env, monkeypatch):
    """scope='agent' (default) only requires trust ≥ 1."""

    async def _trust_1(tenant_id, agent_id, min_level):  # noqa: ARG001
        if min_level > 1:
            return (
                1,
                False,
                f"Error (403): Agent 'alice' (trust_level=1) < required {min_level}.",
            )
        return 1, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_1)
    stub_storage_client(monkeypatch, list_memories_by_filters=[])
    out = await mcp_server.memclaw_list(agent_id="alice")  # scope='agent' by default
    assert "FORBIDDEN" not in as_text(out)
    payload = parse_envelope(out)
    assert payload["scope"] == "agent"


async def test_list_scope_fleet_own_allowed_at_trust_1(mcp_env, monkeypatch):
    """scope='fleet' targeting the caller's OWN fleet is allowed at trust ≥ 1
    (spec: L1 = read within own fleet). An omitted fleet_id is pinned to the
    caller's home fleet so the read can't fan out to other fleets' rows."""

    async def _trust_1(tenant_id, agent_id, min_level):  # noqa: ARG001
        if min_level > 1:
            return (
                1,
                False,
                f"Error (403): Agent 'alice' (trust_level=1) < required {min_level}.",
            )
        return 1, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_1)
    sc = stub_storage_client(
        monkeypatch,
        get_agent={"fleet_id": "RND", "trust_level": 1},
        list_memories_by_filters=[],
    )
    out = await mcp_server.memclaw_list(agent_id="alice", scope="fleet")
    assert "FORBIDDEN" not in as_text(out)
    # Omitted fleet_id is pinned to the caller's home fleet.
    payload = sc.list_memories_by_filters.await_args.args[0]
    assert payload["fleet_id"] == "RND"


async def test_list_scope_fleet_cross_blocked_at_trust_1(mcp_env, monkeypatch):
    """scope='fleet' targeting a DIFFERENT fleet still requires trust ≥ 2."""

    async def _trust_1(tenant_id, agent_id, min_level):  # noqa: ARG001
        if min_level > 1:
            return (
                1,
                False,
                f"Error (403): Agent 'alice' (trust_level=1) < required {min_level}.",
            )
        return 1, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_1)
    stub_storage_client(monkeypatch, get_agent={"fleet_id": "RND", "trust_level": 1})
    out = await mcp_server.memclaw_list(agent_id="alice", scope="fleet", fleet_id="OTHER")
    assert "FORBIDDEN" in as_text(out)
    assert "trust_level=1" in as_text(out)


async def test_list_scope_all_blocked_at_trust_1(mcp_env, monkeypatch):
    """scope='all' requires trust ≥ 2; trust-1 agent is rejected."""

    async def _trust_1(tenant_id, agent_id, min_level):  # noqa: ARG001
        if min_level > 1:
            return (
                1,
                False,
                f"Error (403): Agent 'alice' (trust_level=1) < required {min_level}.",
            )
        return 1, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_1)
    out = await mcp_server.memclaw_list(agent_id="alice", scope="all")
    assert "FORBIDDEN" in as_text(out)


async def test_resolve_read_fleet_gate_scope_agent_no_lookup(monkeypatch):
    """scope='agent' returns (1, fleet_id) without any agent lookup."""
    sc = stub_storage_client(monkeypatch)  # get_agent would blow up if awaited
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "alice", "agent", None)
    assert (lvl, fleet) == (1, None)
    sc.get_agent.assert_not_called()


async def test_resolve_read_fleet_gate_scope_all_is_l2_no_lookup(monkeypatch):
    """scope='all' is always cross-fleet (L2) and needs no lookup."""
    sc = stub_storage_client(monkeypatch)
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "alice", "all", None)
    assert lvl == 2
    sc.get_agent.assert_not_called()


async def test_resolve_read_fleet_gate_own_fleet_pins_and_is_l1(monkeypatch):
    """scope='fleet' with no fleet_id + constrained caller → L1, pinned to home."""
    stub_storage_client(monkeypatch, get_agent={"fleet_id": "RND", "trust_level": 1})
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "alice", "fleet", None)
    assert (lvl, fleet) == (1, "RND")


async def test_resolve_read_fleet_gate_explicit_own_fleet_is_l1(monkeypatch):
    """scope='fleet' naming the caller's own fleet → L1, unchanged."""
    stub_storage_client(monkeypatch, get_agent={"fleet_id": "RND", "trust_level": 1})
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "alice", "fleet", "RND")
    assert (lvl, fleet) == (1, "RND")


async def test_resolve_read_fleet_gate_different_fleet_is_l2(monkeypatch):
    """scope='fleet' naming a DIFFERENT fleet → L2, fleet_id preserved."""
    stub_storage_client(monkeypatch, get_agent={"fleet_id": "RND", "trust_level": 1})
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "alice", "fleet", "OTHER")
    assert (lvl, fleet) == (2, "OTHER")


async def test_resolve_read_fleet_gate_trusted_caller_not_pinned(monkeypatch):
    """A trust-≥2 caller that omits fleet_id is NOT pinned; the unfiltered
    cross-fleet fan-out is gated at L2 (not L1) so the enforcement bar matches
    the access granted and the demotion-race window is closed."""
    stub_storage_client(monkeypatch, get_agent={"fleet_id": "RND", "trust_level": 2})
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "alice", "fleet", None)
    assert (lvl, fleet) == (2, None)


async def test_resolve_read_fleet_gate_unknown_agent_soft_passes(monkeypatch):
    """Unregistered caller (no row) soft-passes at L1 with no pin (matches recall)."""
    stub_storage_client(monkeypatch, get_agent=None)
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "ghost", "fleet", None)
    assert (lvl, fleet) == (1, None)


async def test_list_fleet_gate_storage_error_returns_structured_envelope(mcp_env, monkeypatch):
    """A get_agent failure in the scope='fleet' gate is caught at the call site
    and surfaced as a structured error, not an unhandled raise."""
    sc = stub_storage_client(monkeypatch)
    sc.get_agent = AsyncMock(side_effect=RuntimeError("storage down"))
    out = await mcp_server.memclaw_list(agent_id="alice", scope="fleet")
    assert "INTERNAL_ERROR" in as_text(out)


async def test_list_fleet_gate_httpx_error_returns_storage_envelope(mcp_env, monkeypatch):
    """An httpx.HTTPStatusError from the gate maps through _storage_error_envelope."""
    sc = stub_storage_client(monkeypatch)
    req = httpx.Request("GET", "http://storage/agents/alice")
    resp = httpx.Response(503, request=req)
    sc.get_agent = AsyncMock(
        side_effect=httpx.HTTPStatusError("unavailable", request=req, response=resp)
    )
    out = await mcp_server.memclaw_list(agent_id="alice", scope="fleet")
    # Structured error envelope (not an unhandled exception).
    assert "error" in as_text(out).lower()


async def test_stats_fleet_gate_storage_error_returns_structured_envelope(mcp_env, monkeypatch):
    """Same call-site guard for memclaw_stats: gate failures → INTERNAL_ERROR."""
    sc = stub_storage_client(monkeypatch)
    sc.get_agent = AsyncMock(side_effect=RuntimeError("storage down"))
    out = await mcp_server.memclaw_stats(agent_id="alice", scope="fleet")
    assert "INTERNAL_ERROR" in as_text(out)


async def test_stats_fleet_gate_httpx_error_returns_storage_envelope(mcp_env, monkeypatch):
    """memclaw_stats maps a gate httpx error through _storage_error_envelope too
    (preserving upstream status), matching memclaw_list — not a flat INTERNAL_ERROR."""
    sc = stub_storage_client(monkeypatch)
    req = httpx.Request("GET", "http://storage/agents/alice")
    resp = httpx.Response(503, request=req)
    sc.get_agent = AsyncMock(
        side_effect=httpx.HTTPStatusError("unavailable", request=req, response=resp)
    )
    out = await mcp_server.memclaw_stats(agent_id="alice", scope="fleet")
    assert "error" in as_text(out).lower()


async def test_resolve_read_fleet_gate_registered_fleetless_no_fleet_is_l2(monkeypatch):
    """Registered trust-1 caller with NO home fleet and no fleet_id param can't
    prove fleet membership → L2 (require_trust then rejects), not an unfiltered
    L1 scan across all fleets' scope_team rows."""
    stub_storage_client(monkeypatch, get_agent={"fleet_id": None, "trust_level": 1})
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "alice", "fleet", None)
    assert (lvl, fleet) == (2, None)


async def test_resolve_read_fleet_gate_unknown_agent_explicit_fleet_is_l2(monkeypatch):
    """Unregistered caller with explicit fleet_id cannot confirm ownership → L2."""
    stub_storage_client(monkeypatch, get_agent=None)
    lvl, fleet = await mcp_server._resolve_read_fleet_gate("t", "ghost", "fleet", "SOME_FLEET")
    assert (lvl, fleet) == (2, "SOME_FLEET")


async def test_list_invalid_scope(mcp_env):
    out = await mcp_server.memclaw_list(scope="everywhere")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid scope" in as_text(out)


async def test_list_scope_agent_rejects_foreign_written_by(mcp_env):
    """scope='agent' + written_by != caller returns 422."""
    out = await mcp_server.memclaw_list(
        agent_id="alice", scope="agent", written_by="bob"
    )
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "written_by must be omitted" in as_text(out)


async def test_list_scope_agent_forces_written_by(mcp_env, monkeypatch):
    """scope='agent' forces written_by to the caller's agent_id."""
    sc = stub_storage_client(monkeypatch, list_memories_by_filters=[])
    await mcp_server.memclaw_list(agent_id="alice", scope="agent")
    payload = sc.list_memories_by_filters.await_args.args[0]
    assert payload["written_by"] == "alice"
    # scope='agent' must NOT widen via the readable set.
    assert payload["readable_tenant_ids"] is None
    assert payload["caller_agent_id"] == "alice"


async def test_list_invalid_memory_type(mcp_env):
    out = await mcp_server.memclaw_list(memory_type="chicken")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid memory_type 'chicken'" in as_text(out)


async def test_list_invalid_status(mcp_env):
    out = await mcp_server.memclaw_list(status="fancy")
    assert "INVALID_ARGUMENTS" in as_text(out)


async def test_list_invalid_sort(mcp_env):
    out = await mcp_server.memclaw_list(sort="content")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid sort" in as_text(out)


async def test_list_invalid_order(mcp_env):
    out = await mcp_server.memclaw_list(order="sideways")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "order must be 'asc' or 'desc'" in as_text(out)


async def test_list_cursor_with_non_default_sort_errors(mcp_env):
    out = await mcp_server.memclaw_list(cursor="x", sort="weight")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "cursor pagination requires" in as_text(out)


async def test_list_cursor_with_asc_order_errors(mcp_env):
    out = await mcp_server.memclaw_list(cursor="x", order="asc")
    assert "INVALID_ARGUMENTS" in as_text(out)


async def test_list_invalid_cursor_payload(mcp_env):
    out = await mcp_server.memclaw_list(cursor="@@not-base64@@")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid cursor" in as_text(out)


async def test_list_invalid_created_after_iso(mcp_env):
    out = await mcp_server.memclaw_list(created_after="not-iso")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "created_after must be ISO8601" in as_text(out)


async def test_list_invalid_created_before_iso(mcp_env):
    out = await mcp_server.memclaw_list(created_before="not-iso")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "created_before must be ISO8601" in as_text(out)


async def test_list_happy_path_empty_results(mcp_env, monkeypatch):
    stub_storage_client(monkeypatch, list_memories_by_filters=[])
    out = await mcp_server.memclaw_list()
    payload = parse_envelope(out)
    assert payload == {"count": 0, "results": [], "next_cursor": None, "scope": "agent"}


async def test_list_happy_path_with_rows_and_next_cursor(mcp_env, monkeypatch):
    """Page of 3 with limit=2 → 2 items returned + next_cursor non-null.

    Storage returns dict rows (``limit+1`` over-fetched); the tool slices to
    ``limit`` and builds ``next_cursor`` from the last served row's
    ``created_at`` + ``id``. ``_memory_to_out`` (top-level import on mcp_server)
    accepts a dict row, so patch it there for a deterministic shape."""
    rows = [
        {"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat()}
        for _ in range(3)
    ]
    stub_storage_client(monkeypatch, list_memories_by_filters=rows)
    monkeypatch.setattr(mcp_server, "_memory_to_out", lambda m: _out_stub(m["id"]))
    out = await mcp_server.memclaw_list(limit=2)
    payload = parse_envelope(out)
    assert payload["count"] == 2
    assert len(payload["results"]) == 2
    assert payload["next_cursor"] is not None


async def test_list_include_deleted_requires_trust_3(mcp_env, monkeypatch):
    """Trust 2 + include_deleted=True is silently ignored — core-api sends
    ``include_deleted=False`` to storage (which keeps the deleted_at filter).

    (Fix 2 Phase 4: the deleted_at SQL itself now lives in
    ``PostgresService.memory_list_by_filters``; the trust gate is core-api's, so
    we assert the flag core-api forwards.)"""

    async def _trust_2(tenant_id, agent_id, min_level):  # noqa: ARG001
        return 2, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_2)
    sc = stub_storage_client(monkeypatch, list_memories_by_filters=[])
    await mcp_server.memclaw_list(agent_id="alice", include_deleted=True)
    payload = sc.list_memories_by_filters.await_args.args[0]
    assert payload["include_deleted"] is False


async def test_list_include_deleted_honored_at_trust_3(mcp_env, monkeypatch):
    """Trust 3 with include_deleted=True forwards ``include_deleted=True`` to
    storage (which then drops the deleted_at filter)."""

    async def _trust_3(tenant_id, agent_id, min_level):  # noqa: ARG001
        return 3, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_3)
    sc = stub_storage_client(monkeypatch, list_memories_by_filters=[])
    await mcp_server.memclaw_list(agent_id="admin", include_deleted=True)
    payload = sc.list_memories_by_filters.await_args.args[0]
    assert payload["include_deleted"] is True


async def test_list_auth_failure_shortcircuits(monkeypatch):
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)
    out = await mcp_server.memclaw_list()
    assert out == mcp_server._AUTH_ERROR


async def test_list_limit_clamped_to_1_50(mcp_env, monkeypatch):
    """limit=999 gets clamped to 50; limit=0 gets clamped to 1.

    Fix 2 Phase 4: core-api forwards the CLAMPED ``limit`` in the storage
    payload; ``PostgresService.memory_list_by_filters`` adds the ``+1``
    over-fetch internally. So we assert the clamped value core-api sends."""
    sc = stub_storage_client(monkeypatch, list_memories_by_filters=[])

    await mcp_server.memclaw_list(limit=999)
    assert sc.list_memories_by_filters.await_args.args[0]["limit"] == 50

    await mcp_server.memclaw_list(limit=0)
    assert sc.list_memories_by_filters.await_args.args[0]["limit"] == 1

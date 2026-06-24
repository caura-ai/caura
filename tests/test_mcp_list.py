"""Unit tests for ``memclaw_list`` — non-semantic memory enumeration.

Covers:
- Scope-based trust gating: scope='agent' at trust ≥ 1, scope='fleet'/'all' at trust ≥ 2.
- scope='agent' forces written_by to the caller's agent_id.
- Filter / sort / order validation (422).
- ``include_deleted`` only honored at trust ≥ 3 (silently ignored below).
- Invalid cursor / ISO dates.
- Cursor vs sort/order constraint (only created_at/desc).
- Happy path (zero rows) shape: ``{count, results, next_cursor, scope}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

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


async def test_list_scope_fleet_blocked_at_trust_1(mcp_env, monkeypatch):
    """scope='fleet' requires trust ≥ 2; trust-1 agent is rejected."""

    async def _trust_1(tenant_id, agent_id, min_level):  # noqa: ARG001
        if min_level > 1:
            return (
                1,
                False,
                f"Error (403): Agent 'alice' (trust_level=1) < required {min_level}.",
            )
        return 1, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_1)
    out = await mcp_server.memclaw_list(agent_id="alice", scope="fleet")
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

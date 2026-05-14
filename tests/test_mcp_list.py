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
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import as_text, parse_envelope

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _mock_result(rows):
    """Build a fake SQLAlchemy Result whose .scalars().all() returns `rows`."""
    scalars = MagicMock()
    scalars.all.return_value = rows
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _out_stub(mid: str):
    class _Out:
        def model_dump(self, mode="python"):  # noqa: ARG002
            return {"id": mid, "content": f"memory {mid}"}

    return _Out()


async def test_list_scope_agent_allowed_at_trust_1(mcp_env, monkeypatch):
    """scope='agent' (default) only requires trust ≥ 1."""

    async def _trust_1(db, tenant_id, agent_id, min_level):  # noqa: ARG001
        if min_level > 1:
            return 1, False, f"Error (403): Agent 'alice' (trust_level=1) < required {min_level}."
        return 1, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_1)
    mcp_env["db"].execute.return_value = _mock_result([])
    out = await mcp_server.memclaw_list(agent_id="alice")  # scope='agent' by default
    assert "FORBIDDEN" not in as_text(out)
    payload = parse_envelope(out)
    assert payload["scope"] == "agent"


async def test_list_scope_fleet_blocked_at_trust_1(mcp_env, monkeypatch):
    """scope='fleet' requires trust ≥ 2; trust-1 agent is rejected."""

    async def _trust_1(db, tenant_id, agent_id, min_level):  # noqa: ARG001
        if min_level > 1:
            return 1, False, f"Error (403): Agent 'alice' (trust_level=1) < required {min_level}."
        return 1, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_1)
    out = await mcp_server.memclaw_list(agent_id="alice", scope="fleet")
    assert "FORBIDDEN" in as_text(out)
    assert "trust_level=1" in as_text(out)


async def test_list_scope_all_blocked_at_trust_1(mcp_env, monkeypatch):
    """scope='all' requires trust ≥ 2; trust-1 agent is rejected."""

    async def _trust_1(db, tenant_id, agent_id, min_level):  # noqa: ARG001
        if min_level > 1:
            return 1, False, f"Error (403): Agent 'alice' (trust_level=1) < required {min_level}."
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
    out = await mcp_server.memclaw_list(agent_id="alice", scope="agent", written_by="bob")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "written_by must be omitted" in as_text(out)


async def test_list_scope_agent_forces_written_by(mcp_env, monkeypatch):
    """scope='agent' forces written_by to the caller's agent_id."""
    captured_kwargs = {}

    async def capture_list(db, **kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(mcp_server.memory_repo, "list_by_filters", capture_list)
    mcp_env["db"].execute.return_value = _mock_result([])
    await mcp_server.memclaw_list(agent_id="alice", scope="agent")
    assert captured_kwargs["written_by"] == "alice"


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
    mcp_env["db"].execute.return_value = _mock_result([])
    out = await mcp_server.memclaw_list(cursor="@@not-base64@@")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid cursor" in as_text(out)


async def test_list_invalid_created_after_iso(mcp_env):
    mcp_env["db"].execute.return_value = _mock_result([])
    out = await mcp_server.memclaw_list(created_after="not-iso")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "created_after must be ISO8601" in as_text(out)


async def test_list_invalid_created_before_iso(mcp_env):
    mcp_env["db"].execute.return_value = _mock_result([])
    out = await mcp_server.memclaw_list(created_before="not-iso")
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "created_before must be ISO8601" in as_text(out)


async def test_list_happy_path_empty_results(mcp_env, monkeypatch):
    mcp_env["db"].execute.return_value = _mock_result([])
    monkeypatch.setattr(
        "core_api.services.memory_service._memory_to_out", _out_stub
    )
    out = await mcp_server.memclaw_list()
    payload = parse_envelope(out)
    assert payload == {"count": 0, "results": [], "next_cursor": None, "scope": "agent"}


async def test_list_happy_path_with_rows_and_next_cursor(mcp_env, monkeypatch):
    """Page of 3 with limit=2 → 2 items returned + next_cursor non-null."""
    rows = []
    for i in range(3):
        row = MagicMock()
        row.id = uuid4()
        row.created_at = datetime.now(timezone.utc)
        rows.append(row)
    mcp_env["db"].execute.return_value = _mock_result(rows)
    monkeypatch.setattr(
        "core_api.services.memory_service._memory_to_out",
        lambda m: _out_stub(str(m.id)),
    )
    out = await mcp_server.memclaw_list(limit=2)
    payload = parse_envelope(out)
    assert payload["count"] == 2
    assert len(payload["results"]) == 2
    assert payload["next_cursor"] is not None


async def test_list_include_deleted_requires_trust_3(mcp_env, monkeypatch):
    """Trust 2 + include_deleted=True is silently ignored (filter still applied)."""
    captured_where = []

    async def _trust_2(db, tenant_id, agent_id, min_level):  # noqa: ARG001
        return 2, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_2)

    original_execute = mcp_env["db"].execute

    async def capture_execute(stmt, *a, **kw):
        # Serialize the whereclause to text for a loose structural check.
        captured_where.append(str(stmt.whereclause))
        return _mock_result([])

    mcp_env["db"].execute = capture_execute
    try:
        await mcp_server.memclaw_list(agent_id="alice", include_deleted=True)
    finally:
        mcp_env["db"].execute = original_execute

    # Deleted-at IS NULL filter should still be present for trust 2.
    assert any("deleted_at IS NULL" in w for w in captured_where)


async def test_list_include_deleted_honored_at_trust_3(mcp_env, monkeypatch):
    """Trust 3 with include_deleted=True drops the deleted_at filter."""
    captured_where = []

    async def _trust_3(db, tenant_id, agent_id, min_level):  # noqa: ARG001
        return 3, False, None

    monkeypatch.setattr(mcp_server, "_require_trust", _trust_3)

    async def capture_execute(stmt, *a, **kw):
        captured_where.append(str(stmt.whereclause))
        return _mock_result([])

    mcp_env["db"].execute = capture_execute
    await mcp_server.memclaw_list(agent_id="admin", include_deleted=True)
    # deleted_at IS NULL must not be in the predicate.
    assert all("deleted_at IS NULL" not in w for w in captured_where)


async def test_list_auth_failure_shortcircuits(monkeypatch):
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)
    out = await mcp_server.memclaw_list()
    assert out == mcp_server._AUTH_ERROR


async def test_list_limit_clamped_to_1_50(mcp_env, monkeypatch):
    """limit=999 gets clamped to 50; limit=0 gets clamped to 1."""
    captured_limits = []

    async def capture_execute(stmt, *a, **kw):
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        text = str(compiled)
        # SQL "LIMIT N" — look for the integer after the last LIMIT keyword.
        import re

        m = re.search(r"LIMIT\s+(\d+)", text)
        captured_limits.append(int(m.group(1)) if m else -1)
        return _mock_result([])

    mcp_env["db"].execute = capture_execute
    await mcp_server.memclaw_list(limit=999)
    # Handler passes limit+1 to SQL; clamp is 50 → 51 reaches SQL.
    assert captured_limits[-1] == 51

    await mcp_server.memclaw_list(limit=0)
    assert captured_limits[-1] == 2  # clamped to 1 → passed limit+1=2

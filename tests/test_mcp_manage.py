"""Unit tests for ``memclaw_manage`` (op: read | update | transition | delete | bulk_delete | lineage).

Covers:
- Unknown ``op`` → ``INVALID_ARGUMENTS`` envelope listing the expected ops.
- Invalid memory_id UUID → "Invalid memory_id" error.
- ``op=read`` not found / found.
- ``op=transition`` missing status, invalid status, not-found, happy path.
- ``op=update`` with no fields → "No fields to update"; happy path.
- ``op=delete`` success.
- Service ``HTTPException`` → ``Error (…)`` envelope.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from tests._mcp_test_helpers import (
    as_text,
    parse_envelope,
    stub_storage_client,
    strip_latency,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


VALID_UID = str(uuid4())


def _memory_dict(status="active", agent_id="alice", content="hello"):
    """Storage-client memory row (Fix 2 Phase 4: ``sc.get_memory_for_tenant``
    returns a plain dict, not an ORM row). ``memclaw_manage`` reads it via
    ``.get(...)`` so all the fields the read/transition shaping touches are
    present as dict keys."""
    from datetime import datetime, timezone

    mid = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(mid),
        "agent_id": agent_id,
        "fleet_id": None,
        "memory_type": "fact",
        "status": status,
        "weight": 0.5,
        "visibility": "scope_team",
        "title": "t",
        "summary": "s",
        "content": content,
        "created_at": now,
        "updated_at": now,
        "last_recalled_at": None,
        "recall_count": 0,
        "deleted_at": None,
        "metadata_": {},
    }


async def test_manage_invalid_op_errors(mcp_env):
    out = await mcp_server.memclaw_manage(op="wat", memory_id=VALID_UID)
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "wat" in payload["error"]["message"]
    # sorted() in mcp_server emits the ops in lexicographic order
    assert payload["error"]["details"]["expected_ops"] == [
        "bulk_delete",
        "delete",
        "lineage",
        "read",
        "transition",
        "update",
    ]


async def test_manage_invalid_uuid_errors(mcp_env):
    out = await mcp_server.memclaw_manage(op="read", memory_id="not-a-uuid")
    assert "Invalid memory_id" in as_text(out)


async def test_manage_read_not_found(mcp_env, monkeypatch):
    stub_storage_client(monkeypatch, get_memory_for_tenant=None)
    out = await mcp_server.memclaw_manage(op="read", memory_id=VALID_UID)
    assert "Memory not found" in strip_latency(out)


async def test_manage_read_happy_path(mcp_env, monkeypatch):
    memory = _memory_dict()
    stub_storage_client(monkeypatch, get_memory_for_tenant=memory)
    monkeypatch.setattr(mcp_server, "authorize_memory_access", _async_return(True))
    out = await mcp_server.memclaw_manage(op="read", memory_id=VALID_UID)
    payload = parse_envelope(out)
    assert payload["id"] == memory["id"]
    assert payload["content"] == "hello"
    assert payload["memory_type"] == "fact"


async def test_manage_transition_missing_status_errors(mcp_env):
    out = await mcp_server.memclaw_manage(op="transition", memory_id=VALID_UID)
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "op=transition requires 'status'" in as_text(out)


async def test_manage_transition_invalid_status_errors(mcp_env):
    out = await mcp_server.memclaw_manage(
        op="transition", memory_id=VALID_UID, status="garbage"
    )
    assert "INVALID_ARGUMENTS" in as_text(out)
    assert "Invalid status 'garbage'" in as_text(out)


async def test_manage_transition_not_found(mcp_env, monkeypatch):
    stub_storage_client(monkeypatch, get_memory_for_tenant=None)
    out = await mcp_server.memclaw_manage(
        op="transition", memory_id=VALID_UID, status="archived"
    )
    assert "Memory not found" in strip_latency(out)


async def test_manage_transition_happy_path(mcp_env, monkeypatch):
    memory = _memory_dict(status="active")
    sc = stub_storage_client(
        monkeypatch,
        get_memory_for_tenant=memory,
        update_memory_status=None,
    )
    monkeypatch.setattr(mcp_server, "authorize_memory_access", _async_return(True))
    monkeypatch.setattr(mcp_server, "log_action", _async_return(None))
    out = await mcp_server.memclaw_manage(
        op="transition", memory_id=VALID_UID, status="archived"
    )
    assert "active -> archived" in strip_latency(out)
    sc.update_memory_status.assert_awaited_once()


async def test_manage_update_no_fields_errors(mcp_env):
    out = await mcp_server.memclaw_manage(op="update", memory_id=VALID_UID)
    assert "No fields to update" in strip_latency(out)


async def test_manage_update_happy_path(mcp_env):
    upd = mcp_env["service"]("update_memory")

    class _Out:
        def model_dump(self, mode="python"):  # noqa: ARG002
            return {"id": VALID_UID, "content": "new text"}

    upd.return_value = _Out()
    out = await mcp_server.memclaw_manage(
        op="update", memory_id=VALID_UID, content="new text"
    )
    payload = parse_envelope(out)
    assert payload["content"] == "new text"
    upd.assert_awaited_once()


async def test_manage_delete_happy_path(mcp_env):
    mcp_env["service"]("soft_delete_memory").return_value = None
    out = await mcp_server.memclaw_manage(op="delete", memory_id=VALID_UID)
    assert f"Memory {VALID_UID} deleted" in strip_latency(out)
    mcp_env["service_mocks"]["soft_delete_memory"].assert_awaited_once()


async def test_manage_service_http_exception_envelope(mcp_env):
    mcp_env["service"]("soft_delete_memory").side_effect = HTTPException(
        status_code=403, detail="insufficient trust"
    )
    out = await mcp_server.memclaw_manage(op="delete", memory_id=VALID_UID)
    assert "FORBIDDEN" in as_text(out)
    assert "insufficient trust" in as_text(out)


async def test_manage_auth_failure_shortcircuits(monkeypatch):
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)
    out = await mcp_server.memclaw_manage(op="read", memory_id=VALID_UID)
    assert out == mcp_server._AUTH_ERROR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_return(value):
    async def _fn(*args, **kwargs):  # noqa: ARG001
        return value

    return _fn

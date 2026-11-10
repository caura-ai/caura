"""Destructive and administrative events survive a full process queue."""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.constants import QUARANTINE_REJECT_ACTION, QUARANTINE_RELEASE_ACTION
from core_api.services import audit_service
from core_api.services.audit_queue import AuditEventQueue

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# List the real call sites, including both wire surfaces and every branch of
# the REST status/held-memory decision. Their request setup is unrelated to the
# overflow boundary; read the event arguments from source and exercise the real
# log_action + bounded queue with those arguments.
_CALL_SITES = [
    ("routes/memories.py", "delete_all_memories", "bulk_delete", {}),
    ("routes/memories.py", "bulk_delete_by_ids", "bulk_delete", {}),
    ("routes/memories.py", "delete_memory", "delete", {}),
    ("routes/memories.py", "ingest_undo_endpoint", "ingest_undo", {}),
    ("routes/memories.py", "update_memory_status", "status_update", {"held": False}),
    (
        "routes/memories.py",
        "update_memory_status",
        QUARANTINE_RELEASE_ACTION,
        {"held": True, "status": "active"},
    ),
    (
        "routes/memories.py",
        "update_memory_status",
        QUARANTINE_REJECT_ACTION,
        {"held": True, "status": "cancelled"},
    ),
    ("routes/agents.py", "patch_agent_trust", "agent_trust_update", {}),
    ("routes/agents.py", "update_agent_fleet", "agent_fleet_update", {}),
    ("routes/agents.py", "delete_agent", "delete", {}),
    ("routes/keystones.py", "upsert_keystone", "keystone.set", {}),
    ("routes/keystones.py", "delete_keystone", "keystone.delete", {}),
    ("mcp_server.py", "caura_manage", "bulk_delete", {}),
    ("mcp_server.py", "caura_manage", "status_update", {}),
    ("mcp_server.py", "caura_manage", "delete", {}),
    ("mcp_server.py", "caura_keystones_set", "keystone.set", {}),
    ("mcp_server.py", "caura_keystones_set", "keystone.delete", {}),
]
_CASES = [
    pytest.param(path, function, action, context, id=f"{path}:{function}:{action}")
    for path, function, action, context in _CALL_SITES
]


@lru_cache
def _function(path: str, name: str) -> ast.AST:
    source = Path(__file__).resolve().parents[1] / "core-api/src/core_api" / path
    return next(
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _event_arguments(path, function, expected_action, context) -> dict:
    context = {
        "QUARANTINE_RELEASE_ACTION": QUARANTINE_RELEASE_ACTION,
        "QUARANTINE_REJECT_ACTION": QUARANTINE_REJECT_ACTION,
        **context,
    }
    matches = []
    for node in ast.walk(_function(path, function)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "log_action"
        ):
            continue
        fields = {kw.arg: kw.value for kw in node.keywords}
        # Only literals and the known status conditional are evaluated. Never
        # run request setup, attribute access or calls while finding an event.
        args = {"critical": False}
        for key in ("action", "resource_type", "critical"):
            if key not in fields:
                assert key == "critical"
                continue
            value = fields[key]
            assert all(
                isinstance(
                    part,
                    ast.Constant
                    | ast.Name
                    | ast.Load
                    | ast.IfExp
                    | ast.Compare
                    | ast.Eq,
                )
                for part in ast.walk(value)
            ), f"unexpected event expression at {path}:{node.lineno}"
            args[key] = eval(
                compile(ast.Expression(value), path, "eval"),
                {"__builtins__": {}},
                context,
            )
        if args["action"] == expected_action:
            matches.append(args)
    assert len(matches) == 1, (
        f"expected one {expected_action} call in {path}:{function}"
    )
    return matches[0]


def _boundary(monkeypatch, *, full: bool):
    queue = AuditEventQueue(
        max_queue_size=1,
        flush_threshold=1,
        flush_interval_seconds=1,
        flush_callable=AsyncMock(),
    )
    if full:
        assert queue.enqueue({"client_event_id": "already-pending"})
    storage = MagicMock()
    storage.create_audit_log = AsyncMock()
    monkeypatch.setattr(audit_service, "get_audit_queue", lambda: queue)
    monkeypatch.setattr(audit_service, "get_storage_client", lambda: storage)
    return queue, storage


@pytest.mark.parametrize("path,function,action,context", _CASES)
async def test_named_event_uses_sync_fallback_on_overflow(
    monkeypatch, path, function, action, context
):
    queue, storage = _boundary(monkeypatch, full=True)
    args = _event_arguments(path, function, action, context)
    await audit_service.log_action(tenant_id="tenant", agent_id="actor", **args)

    storage.create_audit_log.assert_awaited_once()
    payload = storage.create_audit_log.await_args.args[0]
    assert payload["tenant_id"] == "tenant"
    assert payload["agent_id"] == "actor"
    assert payload["action"] == action
    assert payload["resource_type"] == args["resource_type"]
    assert payload["client_event_id"]
    assert queue.queue_size == 1
    assert queue.dropped_count == 0, "a recovered event is not a dropped event"


@pytest.mark.parametrize("path,function,action,context", _CASES)
async def test_named_event_stays_async_when_capacity_is_available(
    monkeypatch, path, function, action, context
):
    queue, storage = _boundary(monkeypatch, full=False)
    args = _event_arguments(path, function, action, context)
    await audit_service.log_action(tenant_id="tenant", **args)
    storage.create_audit_log.assert_not_awaited()
    assert queue.queue_size == 1
    assert queue.dropped_count == 0


@pytest.mark.parametrize(
    "action,resource_type",
    [("create", "memory"), ("update", "memory"), ("delete", "unrelated-resource")],
)
async def test_other_events_keep_best_effort_overflow(
    monkeypatch, action, resource_type
):
    queue, storage = _boundary(monkeypatch, full=True)
    await audit_service.log_action(
        tenant_id="tenant", action=action, resource_type=resource_type
    )
    storage.create_audit_log.assert_not_awaited()
    assert queue.queue_size == 1
    assert queue.dropped_count == 1


async def test_critical_overflow_storage_failure_is_logged_without_failing_operation(
    monkeypatch, caplog
):
    queue, storage = _boundary(monkeypatch, full=True)
    storage.create_audit_log.side_effect = RuntimeError("storage unavailable")
    await audit_service.log_action(
        tenant_id="tenant", action="delete", resource_type="memory"
    )
    storage.create_audit_log.assert_awaited_once()
    assert "critical 'delete' event LOST" in caplog.text
    assert queue.queue_size == 1
    assert queue.dropped_count == 0


async def test_inactive_queue_primary_write_failure_still_propagates(monkeypatch):
    _, storage = _boundary(monkeypatch, full=False)
    monkeypatch.setattr(audit_service, "get_audit_queue", lambda: None)
    storage.create_audit_log.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await audit_service.log_action(
            tenant_id="tenant", action="delete", resource_type="memory"
        )
    storage.create_audit_log.assert_awaited_once()

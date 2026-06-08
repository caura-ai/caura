"""C3/C8 — server-reserved memory types rejected at the write boundary.

``outcome`` / ``rule`` / ``insight`` are emitted by internal services
(``evolve_service`` for outcome+rule, ``insights_service`` for insight).
Agent-supplied writes for these types must be rejected at every
external entry point — REST single + bulk and MCP single + batch —
but the *service-layer* helper ``services.memory_service.create_memory``
must remain unaffected (the internal callers go through it directly).

Refusal contract:

* REST: ``HTTPException(status_code=422, …)``; bulk responses name the
  offending row's index (``items[<i>]: …``).
* MCP:  standard error envelope with ``code="INVALID_ARGUMENTS"``; the
  batch path names the offending index.

These tests hit the entry points directly — we don't import the
``_reject_reserved_memory_type`` / ``_refuse_reserved_memory_type``
helpers; that would lock the test to implementation detail rather than
the user-visible behaviour.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from common.enrichment.constants import SERVER_RESERVED_MEMORY_TYPES
from core_api import mcp_server
from core_api.schemas import MemoryOut
from core_api.services import memory_service
from tests._mcp_test_helpers import parse_envelope
from tests.conftest import get_test_auth, uid

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


RESERVED_TYPES = sorted(SERVER_RESERVED_MEMORY_TYPES)  # ["insight", "outcome", "rule"]
ALLOWED_TYPES = ["fact", "semantic"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _OutStub:
    """Minimal stand-in for the dict-able object that ``create_memory`` /
    ``create_memories_bulk`` return on the MCP path.

    The REST happy-path tests use ``_make_memory_out`` instead because the
    FastAPI route serializes the return value against ``MemoryOut`` and
    fails ``ResponseValidationError`` on a partial dict; the MCP path
    just calls ``model_dump`` on whatever the service returned, so the
    stub shape is loose."""

    def __init__(self, mid: str = "m-1"):
        self.mid = mid

    def model_dump(self, mode: str = "python"):  # noqa: ARG002
        return {"id": self.mid, "status": "created"}


def _make_memory_out(
    *, tenant_id: str, agent_id: str, memory_type: str = "fact", content: str = "x"
) -> MemoryOut:
    """Build a fully-populated ``MemoryOut`` for happy-path REST mocks.

    The FastAPI route declares ``response_model=MemoryOut`` and runs the
    serializer on whatever the service returned. We can't hand it a
    sparse stub; this returns a real instance that passes the
    response-model validation cleanly."""
    return MemoryOut(
        id=uuid4(),
        tenant_id=tenant_id,
        fleet_id=None,
        agent_id=agent_id,
        memory_type=memory_type,
        title=None,
        content=content,
        weight=0.5,
        source_uri=None,
        run_id=None,
        metadata=None,
        created_at=datetime.now(timezone.utc),
        expires_at=None,
    )


def _mention_of(reserved_type: str, message: str) -> bool:
    """The refusal message should name the offending type so callers can
    self-correct without grepping the constant. The exact wording is
    implementation detail; tests only assert the type slug appears."""
    return reserved_type in message


# ---------------------------------------------------------------------------
# REST single-write surface — POST /api/v1/memories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reserved_type", RESERVED_TYPES)
async def test_rest_single_rejects_reserved_type(client, reserved_type):
    """Each of the three reserved types yields a 422 with the type
    slug surfaced in the detail. The reject fires at the route layer
    before the service is reached — proven by the 422 status (a real
    create_memory hop would surface 201 or a service-side 4xx)."""
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"agent-supplied {reserved_type} write {uid()}",
            "agent_id": f"agent-{uid()}",
            "memory_type": reserved_type,
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", "")
    # ``detail`` may be a string (HTTPException) or a list (pydantic
    # validation errors). The reject path uses HTTPException, so we
    # expect a string. We don't pin the exact wording — just that the
    # reserved type slug is named somewhere in the message.
    if isinstance(detail, list):
        # Pydantic-shape — concatenate so the substring check works
        # against either shape.
        detail = " ".join(str(e) for e in detail)
    assert _mention_of(reserved_type, detail), (
        f"422 detail did not mention reserved type {reserved_type!r}: {detail!r}"
    )


async def test_rest_single_accepts_allowed_type(client, monkeypatch):
    """``memory_type='fact'`` falls through the boundary check and the
    service layer is called. We patch ``create_memory`` at the route's
    local binding (the route imports it by name at module load) so the
    test stays a unit-test — no DB, no embeddings, no enrichment."""
    from core_api.routes import memories as routes_mem

    tenant_id, headers = get_test_auth()
    agent_id = f"agent-{uid()}"
    create_mock = AsyncMock(
        return_value=_make_memory_out(
            tenant_id=tenant_id, agent_id=agent_id, memory_type="fact"
        )
    )
    monkeypatch.setattr(routes_mem, "create_memory", create_mock)

    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"a durable knowledge claim {uid()}",
            "agent_id": agent_id,
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    create_mock.assert_awaited_once()


async def test_rest_single_accepts_none_memory_type(client, monkeypatch):
    """Omitting ``memory_type`` (auto-classify path) still works — the
    reserved check is a positive-list reject, not a presence requirement."""
    from core_api.routes import memories as routes_mem

    tenant_id, headers = get_test_auth()
    agent_id = f"agent-{uid()}"
    create_mock = AsyncMock(
        return_value=_make_memory_out(
            tenant_id=tenant_id, agent_id=agent_id, memory_type="fact"
        )
    )
    monkeypatch.setattr(routes_mem, "create_memory", create_mock)

    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"auto-classify me {uid()}",
            "agent_id": agent_id,
            # memory_type intentionally omitted
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    create_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# REST bulk-write surface — POST /api/v1/memories/bulk
# ---------------------------------------------------------------------------


async def test_rest_bulk_rejects_reserved_type_in_any_item(client):
    """One offending row in the middle of an otherwise valid batch must
    fail the whole request with 422 — and the detail must name the
    offending row's index (``items[2]`` here)."""
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"bulk-{uid()}",
        "items": [
            {"content": f"first allowed write {uid()}"},
            {"content": f"second allowed write {uid()}"},
            {
                "content": f"server-only outcome write {uid()}",
                "memory_type": "outcome",
            },
            {"content": f"trailing allowed write {uid()}"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": f"reserved-{uid()}"},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", "")
    if isinstance(detail, list):
        detail = " ".join(str(e) for e in detail)
    # The offending index (2) must be surfaced so callers can find the
    # bad row without re-parsing their batch.
    assert "2" in detail, f"bulk reject did not name offending index: {detail!r}"
    assert _mention_of("outcome", detail), (
        f"bulk reject did not name reserved type: {detail!r}"
    )


async def test_rest_bulk_accepts_when_every_item_allowed(client, monkeypatch):
    """A batch with only agent-allowed types passes the boundary check
    and hits the service. We mock the bulk service at the route's local
    binding (same reason as the single-write happy-path test)."""
    from core_api.routes import memories as routes_mem
    from core_api.schemas import BulkMemoryResponse

    async def _stub_bulk(*_args, **_kwargs):
        return BulkMemoryResponse(
            created=2, duplicates=0, errors=0, results=[], bulk_ms=1
        )

    bulk_mock = AsyncMock(side_effect=_stub_bulk)
    monkeypatch.setattr(routes_mem, "create_memories_bulk", bulk_mock)

    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"bulk-ok-{uid()}",
        "items": [
            {"content": f"allowed one {uid()}", "memory_type": "fact"},
            {"content": f"allowed two {uid()}", "memory_type": "semantic"},
        ],
    }
    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": f"allowed-{uid()}"},
    )
    assert resp.status_code in (200, 201, 207), resp.text
    bulk_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# MCP single-write surface — memclaw_write(content=…)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reserved_type", RESERVED_TYPES)
async def test_mcp_single_rejects_reserved_type(mcp_env, reserved_type):
    """MCP single-write rejects each reserved type with an
    ``INVALID_ARGUMENTS`` envelope and never reaches the service."""
    create_mock = mcp_env["service"]("create_memory")
    create_mock.return_value = _OutStub("should-not-be-used")

    out = await mcp_server.memclaw_write(
        content=f"agent {reserved_type} attempt", memory_type=reserved_type
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert _mention_of(reserved_type, payload["error"]["message"])
    # Boundary rejection — service must not be called.
    mcp_env["service_mocks"]["create_memory"].assert_not_awaited()


async def test_mcp_single_accepts_allowed_type(mcp_env):
    """Allowed types fall through and reach ``create_memory``."""
    mcp_env["service"]("create_memory").return_value = _OutStub("m-fact-mcp")

    out = await mcp_server.memclaw_write(content="a fact for MCP", memory_type="fact")
    payload = parse_envelope(out)
    assert payload.get("id") == "m-fact-mcp"
    mcp_env["service_mocks"]["create_memory"].assert_awaited_once()


# ---------------------------------------------------------------------------
# MCP batch-write surface — memclaw_write(items=[…])
# ---------------------------------------------------------------------------


async def test_mcp_batch_rejects_reserved_item_and_names_index(mcp_env):
    """First reserved-type row triggers ``INVALID_ARGUMENTS``; the
    message names the offending index so callers can locate the bad row."""
    bulk_mock = mcp_env["service"]("create_memories_bulk")
    bulk_mock.return_value = _OutStub("should-not-be-used")

    out = await mcp_server.memclaw_write(
        items=[
            {"content": "ok one"},
            {"content": "server rule attempt", "memory_type": "rule"},
            {"content": "ok three"},
        ],
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    msg = payload["error"]["message"]
    assert _mention_of("rule", msg), f"MCP batch reject didn't name type: {msg!r}"
    assert "1" in msg, f"MCP batch reject didn't name offending index: {msg!r}"
    # Boundary rejection — bulk service must not have been called.
    mcp_env["service_mocks"]["create_memories_bulk"].assert_not_awaited()


async def test_mcp_batch_accepts_when_all_items_allowed(mcp_env):
    """All-allowed batch reaches ``create_memories_bulk`` and returns
    the service payload as-is."""
    mcp_env["service"]("create_memories_bulk").return_value = _OutStub("batch-ok")

    out = await mcp_server.memclaw_write(
        items=[
            {"content": "ok one", "memory_type": "fact"},
            {"content": "ok two", "memory_type": "semantic"},
            {"content": "ok three"},  # None / auto-classify
        ],
    )
    payload = parse_envelope(out)
    assert payload.get("id") == "batch-ok"
    mcp_env["service_mocks"]["create_memories_bulk"].assert_awaited_once()


# ---------------------------------------------------------------------------
# Internal bypass — the boundary lives at the route/MCP edge, NOT at the
# service layer. Internal callers (evolve_service, insights_service) keep
# being able to persist outcome/rule/insight rows.
# ---------------------------------------------------------------------------


async def test_constant_shape():
    """The reserved set is exactly the three documented types."""
    assert SERVER_RESERVED_MEMORY_TYPES == frozenset({"outcome", "rule", "insight"})
    # frozenset is the right shape — immutable, hashable, set-semantics
    # for membership tests. If anyone ever swaps it to a tuple/list this
    # fails loudly (membership checks change cost class).
    assert isinstance(SERVER_RESERVED_MEMORY_TYPES, frozenset)


async def test_service_layer_does_not_gate_on_reserved_types():
    """``services.memory_service.create_memory`` source must not
    reference ``SERVER_RESERVED_MEMORY_TYPES`` — the contract is that
    the gating lives at the route/MCP boundary, NOT at the service
    layer. If a future refactor moves the check down into the service,
    internal callers (``evolve_service``, ``insights_service``) would
    suddenly stop being able to persist their rows.

    This is the cheapest possible regression guard for the
    'internal-callers-still-work' half of the contract."""
    src = inspect.getsource(memory_service.create_memory)
    assert "SERVER_RESERVED_MEMORY_TYPES" not in src, (
        "services.memory_service.create_memory references "
        "SERVER_RESERVED_MEMORY_TYPES — the boundary must live at the "
        "route/MCP layer, not the service layer, otherwise internal "
        "callers (evolve_service, insights_service) lose the ability "
        "to persist outcome/rule/insight rows."
    )

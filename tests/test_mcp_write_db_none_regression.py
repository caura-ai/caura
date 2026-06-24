"""Regression: the MCP / STM write path runs ``create_memory`` with ``db=None``.

The MCP ``memclaw_write`` tool opens ``_no_db()`` (yields ``None``) and calls
``create_memory(...)`` — the storage-routed write path. Before Fix 2
Ph5b PR2 (#472), four ``write_fast`` steps (``load_tenant_config``,
``detect_near_duplicate``, ``check_semantic_duplicate``, and
``write_memory_row``'s audit hook) called ``ctx.require_db``, which **raises** on
``db=None``. The runner logged + broke the failing step without re-raising, then
``create_memory`` read the unset ``ctx.data["memory"]`` and surfaced
``KeyError: 'memory'`` to MCP clients as ``Error executing tool memclaw_write:
'memory'`` (loadtest ``mcp-tool-broken-write`` HIGH; prod regression 2026-06-23).

#472 switched those four steps to the nullable ``ctx.db`` (the functions they
call — ``resolve_config``, ``_find_semantic_duplicate``, ``log_action`` — are
storage-routed and ignore ``db``); #474 made ``create_memory`` raise a clean
HTTP 500 instead of masking a failed step as the cryptic ``KeyError``.

No existing test exercised this: ``test_mcp_write.py`` mocks ``create_memory``,
and the ``mcp_env`` fixture patches ``_no_db`` to yield a ``MagicMock`` (not
``None``). The tests below drive the REAL write pipeline with ``db=None`` against
the integration DB, so reintroducing a ``require_db`` in any write step — or
regressing the ``_no_db`` wiring in ``memclaw_write`` — fails here. ``fast`` mode
covers ``load_tenant_config`` + ``detect_near_duplicate`` + ``write_memory_row``;
``strong`` mode additionally covers ``check_semantic_duplicate``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import core_api.services.memory_service as memory_service
from core_api.schemas import MemoryCreate, MemoryOut
from core_api.services.memory_service import create_memory

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Long enough to clear CheckContentLength's minimum-length quality gate.
_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)


@pytest.fixture(autouse=True)
def _use_pipeline_write():
    """The regression is specific to the pipeline write path; pin it on."""
    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    yield
    memory_service._USE_PIPELINE_WRITE = original


def _tenant() -> str:
    # ``test-tenant-%`` rows are auto-cleaned by the conftest schema fixture.
    return f"test-tenant-mcpwrite-{uuid.uuid4().hex[:8]}"


def _make_input(tenant_id: str, content: str, **kwargs) -> MemoryCreate:
    return MemoryCreate(
        tenant_id=tenant_id,
        fleet_id="test-fleet",
        agent_id="test-agent",
        content=content,
        persist=True,
        entity_links=[],
        **kwargs,
    )


@pytest.mark.parametrize("write_mode", ["fast", "strong"])
async def test_create_memory_tolerates_db_none(db, write_mode):
    """``create_memory()`` runs the full write pipeline without a session
    and persists the row — the exact shape the MCP write path uses. ``fast``
    covers ``load_tenant_config`` + ``detect_near_duplicate`` + ``write_memory_row``;
    ``strong`` additionally covers ``check_semantic_duplicate`` (the other
    ``require_db`` site #472 had to switch). The per-mode content keeps the two
    writes distinct so neither trips the other's dedup gate."""
    tenant = _tenant()
    content = f"The Eiffel Tower is in Paris ({write_mode} mode)." + _PADDING

    result = await create_memory(_make_input(tenant, content, write_mode=write_mode)
    )

    assert isinstance(result, MemoryOut)
    assert result.content == content

    # Confirm it actually landed — a separate session reads the committed row.
    row = (
        await db.execute(
            text("SELECT content, agent_id FROM memories WHERE id = :id"),
            {"id": str(result.id)},
        )
    ).first()
    assert row is not None
    assert row.content == content
    assert row.agent_id == "test-agent"


async def test_memclaw_write_mcp_handler_succeeds_with_no_db(monkeypatch):
    """End-to-end through the real ``memclaw_write`` handler: it opens the REAL
    ``_no_db()`` (yields ``None``) and must return a success payload — not the
    ``KeyError: 'memory'`` envelope the prod regression produced. Only the auth /
    identity context is stubbed; ``_no_db``, ``create_memory``,
    ``enforce_fleet_write`` and ``check_and_increment`` all run for real."""
    from core_api import mcp_server
    from tests._mcp_test_helpers import is_error_envelope, parse_envelope

    tenant = _tenant()
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_check_write_scope", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: "test-agent")

    content = "The Pacific is the largest ocean on Earth." + _PADDING
    out = await mcp_server.memclaw_write(
        content=content, agent_id="test-agent", fleet_id="test-fleet"
    )

    assert not is_error_envelope(out)
    payload = parse_envelope(out)
    assert "error" not in payload
    assert payload.get("id")
    assert payload.get("content") == content

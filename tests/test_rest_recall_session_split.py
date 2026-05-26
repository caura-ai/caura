"""Audit P3 regression test for REST ``/api/v1/recall``.

The handler previously held the FastAPI-injected DB session across the
multi-second LLM brief in ``recall_service.recall()``, pinning a pooled
connection. The load-test gate (`report-loadtest-1779774793.md`) caught
the symptom under `slo-p95-recall_brief` and the cascading
`noisy-neighbor-write` regression: a tenant A search storm holds
connections during LLM calls and starves tenant B writes 6.49×.

The fix mirrors the MCP refactor in PR #228: do all DB-bound work
inside the session, ``await db.close()`` to release the pooled
connection, then call ``summarize_memories`` (the no-DB LLM helper).

This test pins the contract: the session must close BEFORE
``summarize_memories`` runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _MemoryStub:
    """Minimal stand-in for a search result Pydantic model."""

    def __init__(self, mid: str = "m-1"):
        self.id = mid
        self.memory_type = "fact"
        self.title = None
        self.content = "x"
        self.status = "active"
        self.ts_valid_start = None

    def model_dump(self, mode: str = "python"):  # noqa: ARG002
        return {"id": self.id}


async def test_recall_endpoint_closes_session_before_brief_llm(monkeypatch):
    """Phase 1 (search) runs against the FastAPI-injected db; the
    handler then calls ``db.close()`` BEFORE ``summarize_memories``
    fires. Captured order:

        db.close-called
        summarize-start

    If a future change moves ``summarize_memories`` ahead of
    ``db.close()``, the order flips and the assertion fails.
    """
    from core_api.routes import memories as routes_mem
    from core_api.schemas import SearchRequest

    events: list[str] = []

    db = MagicMock(name="db")

    async def _track_close():
        events.append("db.close-called")

    db.close = _track_close

    async def _track_summarize(*_args, **_kwargs):
        events.append("summarize-start")
        return {
            "summary": "synthesized",
            "memories": [],
            "memory_count": 0,
            "recall_ms": 1,
        }

    # Patch the late-imports inside the route function. Each is imported
    # via ``from … import …`` at call time, so we patch at the source.
    monkeypatch.setattr(
        "core_api.services.memory_service.search_memories",
        AsyncMock(return_value=[_MemoryStub("m-1")]),
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace(recall_boost=False, graph_expand=False)),
    )
    monkeypatch.setattr(
        "core_api.services.recall_service.summarize_memories",
        _track_summarize,
    )

    # ``check_and_increment`` is awaited on the auth.tenant_id != None
    # branch. Stub to a no-op AsyncMock.
    monkeypatch.setattr(routes_mem, "check_and_increment", AsyncMock())

    auth = SimpleNamespace(
        tenant_id="tenant-A",
        is_cross_tenant_read=False,
        readable_tenant_ids=["tenant-A"],
        enforce_readable_tenant=lambda _tid: None,
    )
    body = SearchRequest(tenant_id="tenant-A", query="probe", top_k=5)
    request = MagicMock()

    # FastAPI applies ``@search_limit`` (rate-limit decorator). The
    # decorator's wrapper checks the rate-limit slot; bypass by
    # accessing the wrapped function directly.
    handler = routes_mem.recall_endpoint
    # `@search_limit` and `@router.post` both wrap the function. Reach
    # through them via __wrapped__ chains until we find the bare coroutine.
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__

    await handler(request=request, body=body, auth=auth, db=db)

    assert "db.close-called" in events, "db.close() was never called"
    assert "summarize-start" in events, "LLM helper never ran"
    close_idx = events.index("db.close-called")
    llm_idx = events.index("summarize-start")
    assert close_idx < llm_idx, (
        f"db.close() ran AFTER summarize_memories (close={close_idx}, llm={llm_idx}) — "
        "P3 fix regressed; pooled connection still held across the LLM brief"
    )

"""Tests for the capability-usage adoption signal.

Covers the in-process aggregator (folding, non-tenant filtering, drain,
interval flush) and both emitters: the REST middleware route→capability
map and the MCP ``call_tool`` wrapper.
"""

import asyncio
import logging

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from core_api.services import capability_usage as cu
from core_api.services.capability_usage import CapabilityUsageAggregator

# (asyncio_mode=auto in pytest.ini runs async tests without an explicit mark.)


# ── Aggregator ──────────────────────────────────────────────────────────


def _new_agg(captured: list[list[dict]]) -> CapabilityUsageAggregator:
    async def _capture(rows):
        captured.append(rows)

    return CapabilityUsageAggregator(
        flush_interval_seconds=999, flush_callable=_capture
    )


def test_record_folds_into_one_bucket():
    agg = _new_agg([])
    for _ in range(3):
        agg.record(capability="recall", transport="mcp", tenant_id="t1", duration_ms=10)
    rows = agg._drain_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["capability"] == "recall"
    assert r["transport"] == "mcp"
    assert r["tenant_id"] == "t1"
    assert r["op"] is None
    assert r["count"] == 3
    assert r["error_count"] == 0
    assert r["duration_ms_sum"] == 30


def test_distinct_keys_stay_separate():
    agg = _new_agg([])
    agg.record(capability="doc", op="search", transport="mcp", tenant_id="t1")
    agg.record(capability="doc", op="write", transport="mcp", tenant_id="t1")
    agg.record(capability="doc", op="search", transport="rest", tenant_id="t1")
    agg.record(capability="doc", op="search", transport="mcp", tenant_id="t2")
    rows = agg._drain_rows()
    # op, transport, and tenant each split the bucket.
    assert len(rows) == 4


def test_error_status_counted():
    agg = _new_agg([])
    agg.record(capability="write", transport="rest", tenant_id="t1", status="ok")
    agg.record(capability="write", transport="rest", tenant_id="t1", status="error")
    rows = agg._drain_rows()
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert rows[0]["error_count"] == 1


def test_non_tenant_callers_are_dropped():
    agg = _new_agg([])
    for tid in ("", None, "__unauthenticated__", "__admin__", "__no_auth__"):
        agg.record(capability="recall", transport="mcp", tenant_id=tid)
    assert agg._drain_rows() == []


def test_drain_clears_buckets():
    agg = _new_agg([])
    agg.record(capability="recall", transport="mcp", tenant_id="t1")
    assert len(agg._drain_rows()) == 1
    # Second drain is empty — buckets were swapped out.
    assert agg._drain_rows() == []


async def test_flush_loop_emits_and_shutdown_flushes():
    captured: list[list[dict]] = []

    async def _capture(rows):
        captured.append(rows)

    agg = CapabilityUsageAggregator(flush_interval_seconds=0.05, flush_callable=_capture)
    await agg.start()
    agg.record(capability="recall", transport="mcp", tenant_id="t1")
    await asyncio.sleep(0.12)  # let at least one interval tick fire
    await agg.stop(timeout=2.0)
    flat = [r for batch in captured for r in batch]
    assert any(r["capability"] == "recall" and r["count"] >= 1 for r in flat)


async def test_record_usage_noop_when_unwired():
    # No aggregator bound → record_usage must be a silent no-op (no raise).
    cu.set_aggregator(None)
    cu.record_usage(capability="recall", transport="mcp", tenant_id="t1")


async def test_default_flush_routes_to_storage_with_iso_ts_bucket(monkeypatch):
    """``_default_flush`` posts rows through the storage client (no direct DB
    session) and ISO-formats the ``datetime`` ``ts_bucket`` so the JSON payload
    is serializable; the storage endpoint coerces it back to datetime."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock

    bucket = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    rows = [
        {
            "tenant_id": "t1",
            "capability": "recall",
            "op": None,
            "transport": "mcp",
            "ts_bucket": bucket,
            "count": 3,
            "error_count": 0,
            "duration_ms_sum": 30,
        }
    ]

    sc = MagicMock()
    sc.flush_capability_usage = AsyncMock(return_value=1)
    monkeypatch.setattr(cu, "get_storage_client", lambda: sc)

    await cu._default_flush(rows)

    sc.flush_capability_usage.assert_awaited_once()
    sent = sc.flush_capability_usage.await_args.args[0]
    assert sent[0]["ts_bucket"] == bucket.isoformat()
    assert isinstance(sent[0]["ts_bucket"], str)
    # other fields pass through unchanged
    assert sent[0]["tenant_id"] == "t1"
    assert sent[0]["count"] == 3


# ── REST emitter (middleware route→capability map) ──────────────────────


async def test_rest_middleware_records_mapped_capability(monkeypatch):
    from core_api.middleware.request_observation import RequestObservationMiddleware

    calls: list[dict] = []
    monkeypatch.setattr(
        "core_api.middleware.request_observation.record_usage",
        lambda **kw: calls.append(kw),
    )

    app = FastAPI()
    app.add_middleware(RequestObservationMiddleware)

    @app.post("/api/v1/recall")
    async def recall(request: Request):
        request.state.tenant_id = "t-rest"
        return {"ok": True}

    @app.get("/api/v1/unmapped")
    async def unmapped():
        return {"ok": True}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/v1/recall")
        await c.get("/api/v1/unmapped")

    # Only the mapped route records; unmapped is silent.
    assert len(calls) == 1
    k = calls[0]
    assert k["capability"] == "recall"
    assert k["transport"] == "rest"
    assert k["tenant_id"] == "t-rest"
    assert k["status"] == "ok"


# ── MCP emitter (call_tool wrapper) ─────────────────────────────────────


async def test_mcp_call_tool_records_capability_and_op(monkeypatch):
    import core_api.mcp_server as mcp_server

    calls: list[dict] = []
    monkeypatch.setattr(
        mcp_server, "record_usage", lambda **kw: calls.append(kw)
    )
    # Authenticated tenant in the MCP context var the wrapper reads.
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: "t-mcp")

    server = mcp_server._InstrumentedFastMCP(name="test")

    @server.tool(name="memclaw_doc")
    async def doc_tool(op: str) -> str:
        return f"ran {op}"

    await server.call_tool("memclaw_doc", {"op": "search"})

    assert len(calls) == 1
    k = calls[0]
    assert k["capability"] == "doc"   # memclaw_ prefix stripped
    assert k["op"] == "search"
    assert k["transport"] == "mcp"
    assert k["tenant_id"] == "t-mcp"
    assert k["status"] == "ok"


async def test_mcp_call_tool_records_error_on_raise(monkeypatch):
    import core_api.mcp_server as mcp_server

    calls: list[dict] = []
    monkeypatch.setattr(mcp_server, "record_usage", lambda **kw: calls.append(kw))
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: "t-mcp")

    server = mcp_server._InstrumentedFastMCP(name="test")

    @server.tool(name="memclaw_write")
    async def boom() -> str:
        raise RuntimeError("kaboom")

    with pytest.raises(Exception):
        await server.call_tool("memclaw_write", {})

    assert len(calls) == 1
    assert calls[0]["capability"] == "write"
    assert calls[0]["status"] == "error"

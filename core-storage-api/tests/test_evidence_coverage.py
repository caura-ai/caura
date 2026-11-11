"""Evidence coverage's storage half, against a live PostgreSQL.

Each rule the two numbers rest on gets a row that fails exactly that rule: a
placeholder agent, a missing trace, a ``text-input`` or benchmark uri. Recall
candidates count once per return, only when returned, only in the window, and
a candidate whose memory is gone counts as carrying nothing.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"


def _embedding(content: str) -> list[float]:
    h = hashlib.sha256(content.encode()).digest()
    return [((h[i % len(h)] / 255.0) - 0.5) for i in range(1024)]


async def _memory(client: AsyncClient, tenant: str, agent: str, **evidence: str) -> str:
    content = f"evidence coverage test {tenant} {uuid.uuid4().hex}"
    r = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant,
            "agent_id": agent,
            "memory_type": "fact",
            "content": content,
            "embedding": _embedding(content),
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "weight": 0.7,
            "visibility": "scope_team",
            **evidence,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _sql(statement: str, **params: object) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _recall(client: AsyncClient, tenant: str, returned: list[str], near_misses: list[str] = ()) -> None:
    candidates = [{"rank": i, "memory_id": m, "returned": True} for i, m in enumerate(returned)]
    candidates += [{"rank": 100 + i, "memory_id": m, "returned": False} for i, m in enumerate(near_misses)]
    r = await client.post(
        f"{PREFIX}/memories/recall-log",
        json={"event": {"tenant_id": tenant, "source": "mcp_recall"}, "candidates": candidates},
    )
    assert r.status_code == 200, r.text


async def _coverage(client: AsyncClient, tenant: str) -> dict:
    now = datetime.now(UTC)
    r = await client.post(
        f"{PREFIX}/value/evidence-coverage",
        params={"tenant_id": tenant},
        json={
            "start": (now - timedelta(days=30)).isoformat(),
            "end": (now + timedelta(minutes=1)).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_each_rule_excludes_exactly_its_rows(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _memory(client, tenant, "agent-1", trace_id="mcp-s1", tool_call_id="call_1")
    await _memory(client, tenant, "agent-1", trace_id="mcp-s1", source_uri="https://example.com/doc")
    await _memory(client, tenant, "agent-1", trace_id="mcp-s1", source_uri="text-input")
    await _memory(client, tenant, "agent-1", trace_id="mcp-s1", source_uri="LongMemEval://sess-1/turn-2")
    await _memory(client, tenant, "mcp-agent", trace_id="mcp-s1")
    await _memory(client, tenant, "main", trace_id="mcp-s1")
    await _memory(client, tenant, "agent-1", source_uri="upload:report.pdf")
    await _memory(client, tenant, "agent-1", trace_id="  ")
    old = await _memory(client, tenant, "agent-1", trace_id="mcp-s0")
    await _sql("UPDATE memories SET created_at = now() - interval '90 days' WHERE id = :id", id=old)

    body = await _coverage(client, tenant)
    m = body["memories"]
    assert m["rows"] == 9
    assert m["with_agent"] == 7  # not mcp-agent, not main
    assert m["with_trace_id"] == 7  # not untraced, not the blank one
    assert m["provenance"] == 5  # full, uri, pasted, bench, old
    assert m["with_tool_call_id"] == 1
    assert m["with_real_source_uri"] == 2  # https and upload:, not text-input or longmemeval
    assert m["non_evidence_source_uri"] == 2
    assert m["external_evidence"] == 3
    assert body["memories_in_window"]["rows"] == 8
    assert body["memories_in_window"]["provenance"] == 4
    assert body["recall"]["returned"] == 0 and body["recall"]["rows"] == 0
    assert body["verification"] == {"unverified": 9}


async def test_recall_counts_each_return_in_the_window_and_gone_memories_carry_nothing(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    other = f"t-{uuid.uuid4().hex[:8]}"
    good = await _memory(client, tenant, "agent-1", trace_id="mcp-s1", tool_call_id="call_1")
    bare = await _memory(client, tenant, "mcp-agent")
    foreign = await _memory(client, other, "agent-1", trace_id="mcp-s9", tool_call_id="call_9")
    await _recall(client, tenant, [good, bare], near_misses=[bare])
    await _recall(client, tenant, [good, str(uuid.uuid4())])
    # Another tenant's recall of its own memory, and this tenant's recall
    # pointing at it: neither lends this tenant evidence.
    await _recall(client, other, [foreign])
    await _recall(client, tenant, [foreign])
    stale = await _memory(client, tenant, "agent-1", trace_id="mcp-s1", tool_call_id="call_2")
    await _recall(client, tenant, [stale])
    await _sql(
        "UPDATE recall_event SET ts = now() - interval '60 days' WHERE id IN "
        "(SELECT recall_event_id FROM recall_candidate WHERE memory_id = :id)",
        id=stale,
    )

    r = (await _coverage(client, tenant))["recall"]
    assert r["events"] == 3
    assert r["events_with_results"] == 3
    assert r["returned"] == 5  # good x2, bare, gone, foreign; not the near-miss
    assert r["memory_gone"] == 2  # the random id, and the other tenant's row
    assert r["rows"] == 3
    assert r["provenance"] == 2 and r["external_evidence"] == 2


async def test_verification_breakdown_and_deleted_rows(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    ids = [await _memory(client, tenant, "agent-1", trace_id="mcp-s1") for _ in range(4)]
    await _sql("UPDATE memories SET verification_status = 'tool_observed' WHERE id = :id", id=ids[0])
    await _sql("UPDATE memories SET verification_status = 'human_verified' WHERE id = :id", id=ids[1])
    await _sql(
        "UPDATE memories SET verification_status = 'disputed', deleted_at = now() WHERE id = :id", id=ids[2]
    )

    body = await _coverage(client, tenant)
    assert body["memories"]["rows"] == 3
    assert body["verification"] == {"unverified": 1, "tool_observed": 1, "human_verified": 1}


async def test_an_inverted_window_is_refused(client: AsyncClient):
    now = datetime.now(UTC).isoformat()
    r = await client.post(
        f"{PREFIX}/value/evidence-coverage", params={"tenant_id": "t"}, json={"start": now, "end": now}
    )
    assert r.status_code == 422

"""A strong write turns its atomic facts into child rows, as a fast write does (L-117).

Enrichment splits content that states several independent claims into
``atomic_facts``, and each becomes a child memory, so a query naming one fact
finds it directly. A fast write does this after its background enrichment; a
deferred deployment does it through the worker and the ``ENRICHED`` consumer. A
strong write runs enrichment on the request path, and nothing on its branch of
``ScheduleBackgroundTasks`` read the facts. The same content therefore gave a
parent plus one row per fact on a fast write, and one row on a strong write.

Owner decision (2026-10-05): strong mode fans out after the commit, like fast
mode. The children copy the row as written, governance verdict included.

Real storage through the in-process bridge. Only the tenant config, the
enrichment LLM and contradiction detection are stubbed. The step's background
tasks are awaited here, because the suite's fixture cancels whatever is left.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import text

from common.enrichment.schema import AtomicFact, EnrichmentResult
from core_api.pipeline.steps.write import schedule_background_tasks
from core_api.schemas import MemoryCreate
from core_api.services.memory_service import create_memory
from core_api.services.organization_settings import ResolvedConfig
from core_storage_api.services.postgres_service import get_read_session
from tests.conftest import new_tenant_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FACTS = (
    "The Lisbon office opens on the first of March.",
    "The ledger service moves to Postgres in the second quarter.",
)
CONTENT = "Two updates from the planning call. " + " ".join(FACTS)


def _config(governance: dict | None = None) -> ResolvedConfig:
    return ResolvedConfig(
        {
            "enrichment": {"enabled": True, "provider": "fake"},
            "entity_extraction": {"enabled": False},
            "governance": governance or {},
        }
    )


async def _strong_write(
    config: ResolvedConfig,
    *,
    facts: tuple[str, ...] = FACTS,
    business_relevance: str = "business",
    **fields,
):
    enrichment = EnrichmentResult(
        llm_ms=5,
        business_relevance=business_relevance,
        atomic_facts=[AtomicFact(content=f) for f in facts] or None,
    )
    scheduled: list[asyncio.Future] = []

    def _track(coro):
        task = asyncio.ensure_future(coro)
        scheduled.append(task)
        return task

    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=config),
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            AsyncMock(return_value=enrichment),
        ),
        patch(
            "core_api.services.contradiction.run_contradiction_detection",
            AsyncMock(),
        ),
        patch.object(schedule_background_tasks, "track_task", _track),
    ):
        out = await create_memory(
            MemoryCreate(
                tenant_id=new_tenant_id(),
                agent_id="writer",
                content=CONTENT,
                write_mode="strong",
                **fields,
            )
        )
        await asyncio.gather(*scheduled)
    return out


async def _children(parent_id: UUID) -> list[dict]:
    """The live rows derived from ``parent_id``, read from the table so the
    oracle is not the code under test."""
    async with get_read_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT content, visibility, weight, run_id, source_uri,"
                    " ts_valid_start, metadata->>'source' AS source"
                    " FROM memories WHERE metadata->>'parent_memory_id' = :pid"
                    " AND deleted_at IS NULL"
                ),
                {"pid": str(parent_id)},
            )
        ).mappings()
        return [dict(row) for row in rows]


async def test_a_strong_write_fans_its_atomic_facts_out():
    start = datetime(2026, 3, 1, tzinfo=UTC)
    out = await _strong_write(
        _config(),
        weight=0.3,
        run_id="run-l117",
        source_uri="https://example.com/planning-call",
        ts_valid_start=start,
    )

    children = await _children(out.id)
    assert sorted(c["content"] for c in children) == sorted(FACTS)
    for child in children:
        assert child["source"] == "atomic_fact_fanout"
        assert child["visibility"] == out.visibility
        assert child["weight"] == pytest.approx(0.3)
        assert (child["run_id"], child["source_uri"]) == (
            "run-l117",
            "https://example.com/planning-call",
        )
        assert child["ts_valid_start"] == start


async def test_the_children_keep_the_verdict_governance_gave_the_parent():
    """``keep_private`` narrows the parent before the write; the children must
    not republish what it narrowed (#808)."""
    out = await _strong_write(
        _config({"non_business": {"enabled": True, "disposition": "keep_private"}}),
        business_relevance="personal",
    )

    assert out.visibility == "scope_agent"
    children = await _children(out.id)
    assert len(children) == len(FACTS)
    assert {c["visibility"] for c in children} == {"scope_agent"}


async def test_a_strong_write_without_facts_stays_one_row():
    out = await _strong_write(_config(), facts=())

    assert await _children(out.id) == []

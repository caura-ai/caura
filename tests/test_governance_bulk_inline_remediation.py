"""H-18, bulk/ingest half — the LLM governance verdict on the bulk write path.

``create_memories_bulk`` (which the ingest path also uses) enriches
SYNCHRONOUSLY when ``settings.inline_enrichment`` is true and persists the LLM's
``contains_pii`` / ``business_relevance`` into each row's metadata — then nothing
acted on them. Only the DEFERRED branch published an event that the worker's
consumer later remediated, so on an inline deployment a tenant configured to
drop PII or personal content had that policy silently skipped for every
bulk-created memory.

That is the same defect as the single-write inline path, on a second entry point,
and it is why this file exists alongside
``test_governance_inline_fast_remediation.py``.

Two things make the gap easy to miss, and both are pinned below:

* The deterministic gate at the top of ``create_memories_bulk`` DOES run and DOES
  reject pattern-detectable PII pre-write. So the path looks governed. It cannot
  see the LLM's free-form judgement, which is the whole point of the remediation.
* The content used here is deliberately benign — no address, card or key — so it
  sails through that deterministic scan and the drop under test can only be
  attributable to the LLM verdict.

These run against the real database and assert the row's actual ``deleted_at``,
rather than asserting a mock was called: the claim is that the row goes away.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from core_api.config import settings
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
from core_api.services.memory_service import create_memories_bulk
from core_api.services.organization_settings import ResolvedConfig

# Long enough to clear CheckContentLength's minimum-length quality gate, and
# deliberately free of anything the deterministic PII scanner matches.
_PADDING = " This memory carries enough surrounding context to pass the content-length gate."


def _cfg(*, pii=False, pii_action="drop", nb=False, nb_disposition="drop") -> ResolvedConfig:
    return ResolvedConfig(
        {
            "enrichment": {"enabled": True, "provider": "fake"},
            "entity_extraction": {"enabled": False},
            "governance": {
                "pii": {"enabled": pii, "action": pii_action},
                "non_business": {"enabled": nb, "disposition": nb_disposition},
            },
        }
    )


def _enrichment(**over):
    base = dict(
        memory_type="fact",
        weight=0.5,
        status="active",
        title="",
        summary="",
        tags=[],
        llm_ms=12,
        contains_pii=False,
        pii_types=[],
        business_relevance="business",
        retrieval_hint="",
        ts_valid_start=None,
        ts_valid_end=None,
        atomic_facts=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


async def _deleted_at(engine, memory_id):
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT deleted_at FROM memories WHERE id = :i"), {"i": memory_id}
            )
        ).first()
    assert row is not None, f"memory {memory_id} not found"
    return row[0]


async def _write_one(monkeypatch, *, cfg, enrichment):
    """Bulk-write a single benign item under an INLINE deployment.

    Returns the created memory id once every background task it spawned has
    finished — the remediation is scheduled through ``track_task`` alongside the
    other per-row fan-out, so the assertion has to wait for it rather than race.
    """
    monkeypatch.setattr(settings, "deployment_mode", "inline")
    assert settings.inline_enrichment

    req = BulkMemoryCreate(
        # ``test-tenant-%`` rows are auto-cleaned by the conftest schema fixture.
        tenant_id=f"test-tenant-govbulk-{uuid.uuid4().hex[:8]}",
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=[BulkMemoryItem(content="Quarterly planning notes." + _PADDING)],
    )

    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=cfg),
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
    ):
        resp = await create_memories_bulk(req, bulk_attempt_id=uuid.uuid4().hex)
        assert [r.status for r in resp.results] == ["created"], resp.results
        memory_id = resp.results[0].id

        # Drain the fan-out. ``track_task`` registers each task in this set and
        # discards it on completion, so gathering a snapshot waits for exactly
        # the tasks this write scheduled.
        from core_api.tasks import _background_tasks

        if _background_tasks:
            await asyncio.gather(*list(_background_tasks), return_exceptions=True)

    return memory_id


async def test_bulk_inline_write_drops_a_pii_memory_when_configured(monkeypatch, _engine):
    """The gap: an LLM PII verdict must soft-delete a bulk-created row.

    Fails pre-fix — the row stays live because nothing consumed the verdict.
    """
    memory_id = await _write_one(
        monkeypatch,
        cfg=_cfg(pii=True, pii_action="drop"),
        enrichment=_enrichment(contains_pii=True, pii_types=["email"]),
    )
    assert await _deleted_at(_engine, memory_id) is not None, (
        "bulk-created row survived a configured PII drop; the LLM governance "
        "verdict was computed and discarded"
    )


async def test_bulk_inline_write_drops_a_personal_memory_when_configured(monkeypatch, _engine):
    """The non-business half, which additionally needs ``business_relevance``
    to have been persisted by the bulk path."""
    memory_id = await _write_one(
        monkeypatch,
        cfg=_cfg(nb=True, nb_disposition="drop"),
        enrichment=_enrichment(business_relevance="personal"),
    )
    assert await _deleted_at(_engine, memory_id) is not None, (
        "bulk-created row survived a configured non-business drop"
    )


async def test_bulk_inline_write_keeps_clean_content(monkeypatch, _engine):
    """Governance on, verdict clean — the row must survive.

    Guards the drop tests above against passing for the wrong reason (e.g. a
    bulk write that soft-deletes rows for some unrelated reason would make both
    of them green).
    """
    memory_id = await _write_one(
        monkeypatch,
        cfg=_cfg(pii=True, nb=True),
        enrichment=_enrichment(),
    )
    assert await _deleted_at(_engine, memory_id) is None, (
        "governance dropped a row with a clean verdict"
    )


async def test_deterministic_gate_still_rejects_before_the_row_exists(monkeypatch, _engine):
    """The pre-write gate is unchanged: pattern-detectable PII never persists.

    Distinguishes the two mechanisms — this one refuses the item outright rather
    than writing it and remediating afterwards, so there is no row to soft-delete.
    """
    monkeypatch.setattr(settings, "deployment_mode", "inline")
    req = BulkMemoryCreate(
        tenant_id=f"test-tenant-govbulk-{uuid.uuid4().hex[:8]}",
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=[BulkMemoryItem(content="Reach me at alice.smith@example.com." + _PADDING)],
    )
    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_cfg(pii=True, pii_action="drop")),
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=_enrichment()),
        ),
    ):
        resp = await create_memories_bulk(req, bulk_attempt_id=uuid.uuid4().hex)

    assert [r.status for r in resp.results] != ["created"], (
        f"deterministic gate let pattern-detectable PII through: {resp.results}"
    )

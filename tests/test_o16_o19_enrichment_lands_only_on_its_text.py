"""O16 and O19: an enrichment lands only on the text it judged.

An edit is re-enriched (H-07), so a write's enrichment can finish after an edit
has replaced the text it judged. It then wrote the old text's verdicts over the
new, and core-api's ENRICHED handler remediated on them. O16 checked an edit's
run; O19 checks every run, a create's included.

Inline, the run re-reads the row from the writer and does nothing once the text
changed. Deferred, the worker sends the text it enriched as the PATCH's
``expected_content``, and storage applies the PATCH only while the row still
holds that text, checked under the row's lock so an edit cannot land in between.
Otherwise storage answers 409 ``memory_content_changed`` and the worker
announces nothing. The ENRICHED handler skips a run the row no longer matches.

The worker's half is in core-worker's
``tests/test_o16_o19_enrichment_patch_names_its_text.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.events.base import Event
from common.events.memory_enrich_request import MemoryEnrichRequest
from common.events.memory_enriched import MemoryEnriched
from common.events.topics import Topics
from core_api import consumer
from core_api.clients.storage_client import get_storage_client
from core_api.services import memory_service

pytestmark = pytest.mark.asyncio

OLD = "The rollout plan was agreed with the platform team on Monday."
NEW = "The rollout plan moved to Thursday after the platform review."


async def _memory(tenant: str) -> str:
    """A row whose text an edit has replaced: it holds ``NEW``."""
    created = await get_storage_client().create_memory(
        {
            "tenant_id": tenant,
            "agent_id": "o19-agent",
            "memory_type": "fact",
            "content": NEW,
            "status": "active",
            "visibility": "scope_team",
            "metadata_": {"business_relevance": "personal"},
        }
    )
    return str(created["id"])


@pytest.mark.integration
@pytest.mark.parametrize(("expected", "applied"), [(OLD, False), (NEW, True)])
async def test_storage_applies_an_enrichment_only_to_the_text_it_judged(
    storage_http, expected, applied
):
    tenant = f"test-tenant-o16-{uuid.uuid4().hex[:8]}"
    memory_id = await _memory(tenant)

    resp = await storage_http.patch(
        f"/api/v1/storage/memories/{memory_id}",
        json={
            "tenant_id": tenant,
            "title": "Enriched title",
            "metadata_patch": {"business_relevance": "business"},
            "expected_content": expected,
        },
    )

    row = await get_storage_client().get_memory(memory_id, tenant, read=False)
    if applied:
        assert resp.status_code == 200, resp.text
        assert row["title"] == "Enriched title"
        assert row["metadata_"]["business_relevance"] == "business"
    else:
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "memory_content_changed"
        assert row["title"] != "Enriched title"
        assert row["metadata_"]["business_relevance"] == "personal"


async def _inline_run(enriched: str, tenant: str, memory_id: str) -> None:
    """A create's inline run, as the write pipeline schedules it: no pins."""
    enrichment = SimpleNamespace(
        memory_type="fact",
        weight=0.5,
        title="",
        summary="",
        tags=[],
        llm_ms=12,
        contains_pii=False,
        pii_types=[],
        retrieval_hint="",
        business_relevance="business",
        ts_valid_start=None,
        ts_valid_end=None,
        atomic_facts=[],
    )
    config = SimpleNamespace(
        enrichment_enabled=True,
        enrichment_provider="fake",
        semantic_dedup_enabled=False,
        entity_extraction_enabled=False,
        governance_pii=None,
    )
    with (
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=config),
        ),
    ):
        await memory_service._enrich_memory_background(
            uuid.UUID(memory_id), enriched, tenant, None, "o19-agent"
        )


@pytest.mark.integration
@pytest.mark.parametrize(("enriched", "writes"), [(OLD, False), (NEW, True)])
async def test_a_creates_inline_run_on_replaced_text_writes_nothing(enriched, writes):
    tenant = f"test-tenant-o19-{uuid.uuid4().hex[:8]}"
    memory_id = await _memory(tenant)

    await _inline_run(enriched, tenant, memory_id)

    row = await get_storage_client().get_memory(memory_id, tenant, read=False)
    assert row["metadata_"]["business_relevance"] == (
        "business" if writes else "personal"
    )


@pytest.mark.integration
async def test_a_creates_inline_run_reads_the_writer():
    """A lagging replica would make the newest run look stale and skip it."""
    tenant = f"test-tenant-o19-{uuid.uuid4().hex[:8]}"
    memory_id = await _memory(tenant)
    sc = get_storage_client()
    get_memory = sc.get_memory
    reads = []

    async def _get_memory(*args, **kwargs):
        reads.append(kwargs.get("read", True))
        return await get_memory(*args, **kwargs)

    with patch.object(sc, "get_memory", new=_get_memory):
        await _inline_run(NEW, tenant, memory_id)

    # The first read is the content check; nothing else is asserted here.
    assert reads[:1] == [False]


@pytest.mark.parametrize(("announced", "acted_on"), [(OLD, False), (NEW, True)])
async def test_the_enriched_handler_skips_a_run_on_replaced_text(
    monkeypatch, announced, acted_on
):
    """A create's announcement, which carries nothing but the text."""
    memory_id = str(uuid.uuid4())
    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={
            "id": memory_id,
            "tenant_id": "tenant-o19",
            "content": NEW,
            "metadata_": {},
            "embedding": None,
        }
    )
    monkeypatch.setattr(consumer, "get_storage_client", lambda: sc)
    monkeypatch.setattr(consumer, "resolve_config", AsyncMock(return_value=MagicMock()))
    # ``dropped`` ends the handler at remediation, so nothing past it runs.
    remediate = AsyncMock(return_value=MagicMock(dropped=True))
    monkeypatch.setattr(consumer, "remediate_after_enrichment", remediate)

    await consumer.handle_memory_enriched(
        Event(
            event_type=Topics.Memory.ENRICHED,
            tenant_id="tenant-o19",
            payload={
                "memory_id": memory_id,
                "tenant_id": "tenant-o19",
                "content": announced,
            },
        )
    )

    assert remediate.await_count == (1 if acted_on else 0)


@pytest.mark.parametrize("model", [MemoryEnrichRequest, MemoryEnriched])
async def test_every_event_still_asks_backend_v3_30_13_for_the_check(model):
    """That release's worker names the text, and its core-api checks the row,
    only for an event that sets ``current_content_only``. Set on every event, so
    either service of that release checks every run whichever deploys first."""
    event = model(memory_id=uuid.uuid4(), tenant_id="tenant-o19", content=NEW)

    assert event.model_dump(mode="json")["current_content_only"] is True

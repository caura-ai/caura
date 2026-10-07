"""H-07 and M-118: a content edit gets the governance a create gets.

``update_memory`` ran only the deterministic PII gate on edited text. The LLM's
PII verdict, the business/personal disposition and their remediation (drop,
keep private) never saw it, and the verdicts on the OLD text stayed on the row,
so ``contains_pii`` and ``business_relevance`` described text that was gone.

Owner decision (2026-10-05): an edit is re-enriched the way a fast-mode create
is, with remediation, and returns before the verdict lands. ``memory_type`` and
``weight`` stay as they are, because the row does not record whether its caller
set them, and so does a title or date already on the row. The edit's old
children are soft-deleted (M-53) and its re-enrichment derives new atomic facts.

M-118 is on the same lines: a ``flag`` verdict on an edit replaced the patch's
``_system`` with the flag's, dropping ``caller_owned`` and ``embedding_pending``.

Real storage through the in-process bridge, because each claim is about what
the row holds after the edit.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api.clients.storage_client import get_storage_client
from core_api.schemas import MemoryUpdate
from core_api.services import memory_service
from core_api.services.system_metadata import SYSTEM_NAMESPACE
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OLD = "The rollout plan was agreed with the platform team on Monday."
NEW = "The rollout plan moved to Thursday after the platform review."
STALE = {
    "contains_pii": True,
    "pii_types": ["email"],
    "pii_flagged_by": "governance.deterministic",
    "business_relevance": "personal",
}
EDIT_PINS = ["memory_type", "weight"]


def _tenant() -> str:
    return f"test-tenant-h07-{uuid.uuid4().hex[:8]}"


def _config(*, enrichment: bool = True, pii=None) -> SimpleNamespace:
    return SimpleNamespace(
        enrichment_enabled=enrichment,
        enrichment_provider="fake",
        semantic_dedup_enabled=False,
        entity_extraction_enabled=False,
        governance_pii=pii,
    )


async def _memory(tenant: str, **row) -> str:
    """A row whose old text was judged PII-bearing and personal."""
    created = await get_storage_client().create_memory(
        {
            "tenant_id": tenant,
            "agent_id": "h07-agent",
            "memory_type": "fact",
            "content": OLD,
            "status": "active",
            "visibility": "scope_team",
            "metadata_": {
                **STALE,
                # The worker's persisted claims, as the ENRICHED consumer reads them.
                "atomic_facts": [{"content": "The plan was agreed on Monday."}],
                SYSTEM_NAMESPACE: dict(STALE),
            },
            **row,
        }
    )
    return str(created["id"])


async def _edit(tenant: str, memory_id: str, update: MemoryUpdate, config):
    """``update_memory`` with the providers stubbed; returns the enrich scheduler."""
    schedule = AsyncMock()
    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=config),
        ),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
        patch.object(memory_service, "track_task", side_effect=close_scheduled_coro),
        patch.object(memory_service, "_schedule_enrich_or_inline", new=schedule),
    ):
        await memory_service.update_memory(uuid.UUID(memory_id), tenant, update)
    return schedule


async def _stored(tenant: str, memory_id: str) -> dict:
    return await get_storage_client().get_memory(memory_id, tenant, read=False)


async def test_an_edit_clears_the_old_texts_verdicts():
    tenant = _tenant()
    memory_id = await _memory(tenant)

    await _edit(tenant, memory_id, MemoryUpdate(content=NEW), _config())

    md = (await _stored(tenant, memory_id))["metadata_"]
    for key in STALE:
        assert md.get(key) is None, (key, md)
        assert md[SYSTEM_NAMESPACE].get(key) is None, (key, md)
    assert md["enrichment_pending"] is True
    assert md[SYSTEM_NAMESPACE]["enrichment_pending"] is True
    # Or the post-edit ENRICHED event fans out children of the old text.
    assert md.get("atomic_facts") is None, md


@pytest.mark.parametrize(
    ("row", "named", "pins"),
    [
        ({}, {}, EDIT_PINS),
        ({}, {"title": "Caller title"}, sorted([*EDIT_PINS, "title"])),
        # A value already on the row is kept: the row cannot say who set it.
        (
            {"title": "Stored title", "ts_valid_start": "2026-01-05T00:00:00+00:00"},
            {},
            sorted([*EDIT_PINS, "title", "ts_valid_start"]),
        ),
    ],
)
async def test_an_edit_is_re_enriched_and_governed(row, named, pins):
    tenant = _tenant()
    memory_id = await _memory(tenant, **row)

    schedule = await _edit(
        tenant, memory_id, MemoryUpdate(content=NEW, **named), _config()
    )

    schedule.assert_called_once()
    args, kwargs = schedule.call_args
    assert args[0] == uuid.UUID(memory_id)
    assert args[1] == NEW
    assert kwargs["run_governance_remediation"] is True
    assert kwargs["agent_provided_fields"] == pins
    assert kwargs["current_content_only"] is True


async def test_no_re_enrichment_for_a_tenant_without_enrichment():
    tenant = _tenant()
    memory_id = await _memory(tenant)

    schedule = await _edit(
        tenant, memory_id, MemoryUpdate(content=NEW), _config(enrichment=False)
    )

    schedule.assert_not_called()
    md = (await _stored(tenant, memory_id))["metadata_"]
    assert not md.get("enrichment_pending")


async def test_a_flagged_edit_keeps_the_patchs_system_keys():
    """M-118: the flag merges into ``_system``; it does not replace it."""
    tenant = _tenant()
    memory_id = await _memory(tenant)
    pii = SimpleNamespace(enabled=True, enabled_categories=None, action="flag")

    await _edit(
        tenant,
        memory_id,
        MemoryUpdate(
            content=f"{NEW} Questions to jane.roe@example.com.",
            metadata={"summary": "Caller summary"},
        ),
        _config(pii=pii),
    )

    md = (await _stored(tenant, memory_id))["metadata_"]
    system = md[SYSTEM_NAMESPACE]
    assert system["contains_pii"] is True
    assert "summary" in system.get("caller_owned", []), system
    assert system.get("embedding_pending") is True, system
    assert md["summary"] == "Caller summary"


async def test_an_edit_that_clears_metadata_in_replace_mode_is_not_a_500():
    tenant = _tenant()
    memory_id = await _memory(tenant)

    await _edit(
        tenant,
        memory_id,
        MemoryUpdate(content=NEW, metadata=None, metadata_mode="replace"),
        _config(),
    )

    md = (await _stored(tenant, memory_id))["metadata_"]
    assert "contains_pii" not in md, md
    assert md[SYSTEM_NAMESPACE]["embedding_pending"] is True


@pytest.mark.parametrize(
    ("pins", "title"),
    [
        # What an edit sends: the title already on the row stays.
        ([*EDIT_PINS, "title"], "Caller title"),
        # Control: a create's enrichment, which titles the row.
        (None, "Enriched title"),
    ],
)
async def test_an_edits_inline_enrichment_derives_children_and_keeps_its_title(
    pins, title
):
    tenant = _tenant()
    memory_id = await _memory(tenant, title="Caller title")
    enrichment = SimpleNamespace(
        memory_type="decision",
        weight=0.9,
        title="Enriched title",
        summary="Enriched summary",
        tags=[],
        llm_ms=12,
        contains_pii=False,
        pii_types=[],
        retrieval_hint="",
        business_relevance="business",
        ts_valid_start=None,
        ts_valid_end=None,
        atomic_facts=[
            SimpleNamespace(content="The rollout moved to Thursday."),
            SimpleNamespace(content="The platform review set the date."),
        ],
    )
    fan_out = AsyncMock()
    with (
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_config()),
        ),
        patch.object(memory_service, "fan_out_atomic_facts", new=fan_out),
    ):
        await memory_service._enrich_memory_background(
            uuid.UUID(memory_id),
            NEW,
            tenant,
            None,
            "h07-agent",
            agent_provided_fields=pins,
        )

    # M-53: the edit soft-deleted the old children; these are the new text's.
    assert fan_out.await_count == 1
    row = await _stored(tenant, memory_id)
    assert row["title"] == title
    assert row["metadata_"]["business_relevance"] == "business"


@pytest.mark.parametrize(("enriched", "writes"), [(OLD, False), (NEW, True)])
async def test_an_edits_enrichment_of_superseded_text_writes_nothing(enriched, writes):
    """Two edits in quick succession each schedule a run. When the first one
    finished last, it wrote the old text's verdict over the new text's and
    remediated on text the row no longer held."""
    tenant = _tenant()
    memory_id = await _memory(tenant, content=NEW)
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
    with (
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_config()),
        ),
    ):
        await memory_service._enrich_memory_background(
            uuid.UUID(memory_id),
            enriched,
            tenant,
            None,
            "h07-agent",
            agent_provided_fields=EDIT_PINS,
            current_content_only=True,
        )

    md = (await _stored(tenant, memory_id))["metadata_"]
    assert md["business_relevance"] == ("business" if writes else "personal")

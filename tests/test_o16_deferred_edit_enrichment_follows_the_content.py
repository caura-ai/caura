"""O16, H-07's deferred residual: an edit's enrichment lands only on its text.

An edit is re-enriched (H-07). Inline, the run does nothing once a later edit
has replaced the text it judged (``current_content_only``, #1858). Deferred, the
flag stopped at the publish: a worker run that finished after a later edit wrote
the old text's verdicts over the new text, and core-api's ENRICHED handler then
remediated on them.

Now the edit's request carries the flag. The worker sends the text it enriched
as the PATCH's ``expected_content``, and storage applies the PATCH only while
the row still holds that text, checked under the row's lock so a later edit
cannot land in between. Otherwise storage answers 409 ``memory_content_changed``
and the worker announces nothing. The ENRICHED handler skips an edit's run that
the row no longer matches.

The worker's half is in core-worker's
``tests/test_o16_edit_enrichment_follows_the_content.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.events.base import Event
from common.events.topics import Topics
from core_api import consumer
from core_api.clients.storage_client import get_storage_client
from core_api.services import memory_service

pytestmark = pytest.mark.asyncio

OLD = "The rollout plan was agreed with the platform team on Monday."
NEW = "The rollout plan moved to Thursday after the platform review."


@pytest.mark.integration
@pytest.mark.parametrize(("expected", "applied"), [(OLD, False), (NEW, True)])
async def test_storage_applies_an_enrichment_only_to_the_text_it_judged(
    storage_http, expected, applied
):
    tenant = f"test-tenant-o16-{uuid.uuid4().hex[:8]}"
    created = await get_storage_client().create_memory(
        {
            "tenant_id": tenant,
            "agent_id": "o16-agent",
            "memory_type": "fact",
            "content": NEW,
            "status": "active",
            "visibility": "scope_team",
            "metadata_": {"business_relevance": "business"},
        }
    )
    memory_id = str(created["id"])

    resp = await storage_http.patch(
        f"/api/v1/storage/memories/{memory_id}",
        json={
            "tenant_id": tenant,
            "title": "Enriched title",
            "metadata_patch": {"business_relevance": "personal"},
            "expected_content": expected,
        },
    )

    row = await get_storage_client().get_memory(memory_id, tenant, read=False)
    if applied:
        assert resp.status_code == 200, resp.text
        assert row["title"] == "Enriched title"
        assert row["metadata_"]["business_relevance"] == "personal"
    else:
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "memory_content_changed"
        assert row["title"] != "Enriched title"
        assert row["metadata_"]["business_relevance"] == "business"


async def test_an_edits_deferred_enrichment_asks_for_its_text_only():
    """Inline, ``current_content_only`` reached the run. Deferred, the publish
    dropped it, so the worker never heard it."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    with (
        patch.object(memory_service.settings, "deployment_mode", "deferred"),
        patch(
            "common.events.memory_enrich_publisher.get_event_bus",
            return_value=bus,
        ),
    ):
        await memory_service._schedule_enrich_or_inline(
            uuid.uuid4(),
            NEW,
            "tenant-o16",
            None,
            "o16-agent",
            None,
            agent_provided_fields=["memory_type", "weight"],
            run_governance_remediation=True,
            current_content_only=True,
        )

    bus.publish.assert_awaited_once()
    topic, event = bus.publish.await_args.args
    assert topic == Topics.Memory.ENRICH_REQUESTED
    assert event.payload["content"] == NEW
    assert event.payload["current_content_only"] is True


@pytest.mark.parametrize(("announced", "acted_on"), [(OLD, False), (NEW, True)])
async def test_the_enriched_handler_skips_an_edits_run_on_replaced_text(
    monkeypatch, announced, acted_on
):
    memory_id = str(uuid.uuid4())
    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={
            "id": memory_id,
            "tenant_id": "tenant-o16",
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
            tenant_id="tenant-o16",
            payload={
                "memory_id": memory_id,
                "tenant_id": "tenant-o16",
                "content": announced,
                "current_content_only": True,
            },
        )
    )

    assert remediate.await_count == (1 if acted_on else 0)

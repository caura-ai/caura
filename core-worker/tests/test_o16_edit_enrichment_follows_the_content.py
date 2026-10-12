"""O16: an edit's deferred enrichment lands only on the text it judged.

An edit's request says ``current_content_only``. The worker sends the text it
enriched as the PATCH's ``expected_content``. Storage refuses the PATCH with 409
``memory_content_changed`` once a later edit has replaced that text, and the
worker then neither retries nor announces it. Storage's half is in the root
suite's ``tests/test_o16_deferred_edit_enrichment_follows_the_content.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

import core_worker.consumer as consumer
from common.enrichment import EnrichmentResult
from common.events.base import Event
from core_worker.clients.storage_client import update_memory_enrichment

CONTENT = "The rollout plan moved to Thursday after the platform review."


def _event(**payload) -> Event:
    return Event(
        event_type="caura.memory.enrich-requested",
        tenant_id="tenant-A",
        payload={"memory_id": str(uuid4()), "tenant_id": "tenant-A", "content": CONTENT, **payload},
    )


@pytest.fixture
def worker(monkeypatch) -> tuple[AsyncMock, AsyncMock]:
    consumer.configure(lambda: MagicMock(spec=httpx.AsyncClient))
    monkeypatch.setattr(
        consumer, "enrich_memory", AsyncMock(return_value=EnrichmentResult(memory_type="fact"))
    )
    update = AsyncMock(return_value=True)
    publish = AsyncMock()
    monkeypatch.setattr(consumer, "update_memory_enrichment", update)
    monkeypatch.setattr(consumer, "publish_memory_enriched", publish)
    return update, publish


@pytest.mark.parametrize(("current_content_only", "expected_content"), [(True, CONTENT), (False, None)])
async def test_an_edits_patch_names_the_text_it_judged(
    worker, current_content_only, expected_content
) -> None:
    update, _ = worker

    await consumer.handle_enrich_request(_event(current_content_only=current_content_only))

    assert update.await_args.kwargs.get("expected_content") == expected_content


async def test_an_edits_announcement_says_it_is_one(worker) -> None:
    """So core-api's ENRICHED handler can skip it if a later edit lands first."""
    _, publish = worker

    await consumer.handle_enrich_request(_event(current_content_only=True))

    assert publish.await_args.kwargs["current_content_only"] is True


def _client(status_code: int, detail: object) -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    request = httpx.Request("PATCH", "http://storage/api/v1/storage/memories/m")
    client.patch = AsyncMock(
        return_value=httpx.Response(status_code, json={"detail": detail}, request=request)
    )
    return client


async def test_storage_refusing_replaced_text_is_not_landed_or_retried() -> None:
    client = _client(409, {"error": "memory_content_changed", "message": "the memory's content changed"})

    landed = await update_memory_enrichment(
        client, memory_id=uuid4(), tenant_id="tenant-A", fields={"title": "t"}, expected_content=CONTENT
    )

    assert landed is False


async def test_another_conflict_still_raises() -> None:
    """Control: only the named refusal is a skip. Any other 409 nacks, as before."""
    client = _client(409, "Conflict")

    with pytest.raises(httpx.HTTPStatusError):
        await update_memory_enrichment(client, memory_id=uuid4(), tenant_id="tenant-A", fields={"title": "t"})

"""O16 and O19: an enrichment lands only on the text it judged.

Every run, a create's as well as an edit's, sends the text it enriched as the
PATCH's ``expected_content``. Storage refuses the PATCH with 409
``memory_content_changed`` once an edit has replaced that text, and the worker
then neither retries nor announces it. O16 checked an edit's run only, so a
create's run that finished after an edit wrote the original text's verdicts over
the edited text's (O19). Storage's half is in the root suite's
``tests/test_o16_o19_enrichment_lands_only_on_its_text.py``.
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


async def test_a_creates_patch_names_the_text_it_judged(monkeypatch) -> None:
    """A create's request, which carries nothing but the text."""
    consumer.configure(lambda: MagicMock(spec=httpx.AsyncClient))
    monkeypatch.setattr(
        consumer, "enrich_memory", AsyncMock(return_value=EnrichmentResult(memory_type="fact"))
    )
    update = AsyncMock(return_value=True)
    monkeypatch.setattr(consumer, "update_memory_enrichment", update)
    monkeypatch.setattr(consumer, "publish_memory_enriched", AsyncMock())

    await consumer.handle_enrich_request(
        Event(
            event_type="caura.memory.enrich-requested",
            tenant_id="tenant-A",
            payload={"memory_id": str(uuid4()), "tenant_id": "tenant-A", "content": CONTENT},
        )
    )

    assert update.await_args.kwargs.get("expected_content") == CONTENT


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

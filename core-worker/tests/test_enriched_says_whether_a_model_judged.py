"""The ENRICHED event says whether a model judged the text.

When every provider fails, ``enrich_memory`` falls back to the keyword
heuristic (``llm_ms == 0``), which gives no PII or business verdict. core-api
applies the tenant's LLM governance policy from the event, so it has to know
which of the two it got: with no verdict it marks the row and logs it for the
re-run sweep instead of treating it as judged clean.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

import core_worker.consumer as consumer
from common.enrichment import EnrichmentResult
from common.events.base import Event
from common.events.memory_enriched import MemoryEnriched


@pytest.mark.parametrize(("llm_ms", "judged"), [(0, False), (412, True)], ids=["heuristic", "model"])
async def test_the_event_says_whether_a_model_judged_the_text(monkeypatch, llm_ms, judged) -> None:
    consumer.configure(lambda: MagicMock(spec=httpx.AsyncClient))
    monkeypatch.setattr(
        consumer,
        "enrich_memory",
        AsyncMock(return_value=EnrichmentResult(memory_type="fact", llm_ms=llm_ms)),
    )
    monkeypatch.setattr(consumer, "update_memory_enrichment", AsyncMock(return_value=True))
    publish = AsyncMock()
    monkeypatch.setattr(consumer, "publish_memory_enriched", publish)

    await consumer.handle_enrich_request(
        Event(
            event_type="caura.memory.enrich-requested",
            tenant_id="tenant-A",
            payload={"memory_id": str(uuid4()), "tenant_id": "tenant-A", "content": "x"},
        )
    )

    assert publish.await_args.kwargs["llm_verdict"] is judged


def test_an_event_from_an_older_worker_says_nothing() -> None:
    """Absent, not False: an older worker's event must not read as a failure."""
    event = MemoryEnriched(memory_id=uuid4(), tenant_id="tenant-A", content="x")

    assert event.llm_verdict is None

"""``ingest_preview`` parses, chunks and PII-scans a document off the event loop.

All three steps are pure-Python CPU work. At ``DOC_HARD_TOKEN_LIMIT`` that is
about 0.1s to parse a markdown document and about 0.8s to chunk one long
paragraph, measured locally; the PII scan runs every pattern over the whole
text. Run on the event loop, every other request the worker is serving waits
that long. They run in a worker thread instead, and the loop keeps serving.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api.schemas import IngestRequest
from core_api.services import ingest_service, pii_update_gate

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# Three sections: the intro under the H1, then one per H2.
DOC = (
    "# Runbook\n\nThe intro section explains the service.\n\n"
    "## Deploy\n\nThe deploy section says to run the installer.\n\n"
    "## Rollback\n\nThe rollback section says to restore the snapshot.\n"
)


def _serving(served: dict[str, bool], step: str, real):
    """A stand-in for ``real`` that asks the loop to run a callback, then waits
    for it.

    Off the loop the loop is free, so the callback runs at once. On the loop
    nothing else can run until the step returns, so the wait times out and the
    callback is reported as never served.
    """
    loop = asyncio.get_running_loop()

    def stand_in(*args, **kwargs):
        ran = threading.Event()
        loop.call_soon_threadsafe(ran.set)
        served[step] = ran.wait(timeout=2.0)
        return real(*args, **kwargs)

    return stand_in


@pytest.fixture
def offline_preview(monkeypatch):
    async def _config(tenant_id):
        return SimpleNamespace(enrichment_provider="openai", enrichment_enabled=True)

    async def _no_cache(*_args, **_kwargs):
        return []

    async def _chunk(text, focus=None, tenant_config=None, breadcrumb=None):
        return [{"content": f"A fact from: {text[:40]}", "suggested_type": "fact"}]

    monkeypatch.setattr(ingest_service, "resolve_config", _config)
    monkeypatch.setattr(ingest_service, "_find_prior_ingest_by_doc_hash", _no_cache)
    monkeypatch.setattr(ingest_service, "_chunk_content", _chunk)


async def test_the_loop_keeps_serving_while_a_document_is_parsed_and_chunked(
    monkeypatch, offline_preview
):
    served: dict[str, bool] = {}
    monkeypatch.setattr(
        ingest_service, "parse", _serving(served, "parse", ingest_service.parse)
    )
    monkeypatch.setattr(
        ingest_service,
        "chunk_blocks",
        _serving(served, "chunk_blocks", ingest_service.chunk_blocks),
    )

    resp = await ingest_service.ingest_preview(
        IngestRequest(tenant_id="t1", content=DOC)
    )

    assert served == {"parse": True, "chunk_blocks": True}, (
        f"the event loop was blocked while these ran: {[s for s, ok in served.items() if not ok]}"
    )
    # The thread's result is what the preview used.
    assert resp["sections"] == 3
    assert len(resp["facts"]) == 3


async def test_the_loop_keeps_serving_while_a_document_is_pii_scanned_and_masked(
    monkeypatch, offline_preview
):
    gov = SimpleNamespace(enabled=True, action="mask", enabled_categories=None)
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace(governance_pii=gov)),
    )
    monkeypatch.setattr(pii_update_gate, "emit_governance_audit", AsyncMock())
    served: dict[str, bool] = {}
    monkeypatch.setattr(
        pii_update_gate, "scan", _serving(served, "scan", pii_update_gate.scan)
    )
    monkeypatch.setattr(
        pii_update_gate, "mask", _serving(served, "mask", pii_update_gate.mask)
    )

    await ingest_service.ingest_preview(
        IngestRequest(
            tenant_id="t1", content=DOC + "\nEscalate to john.doe@example.com.\n"
        )
    )

    assert served == {"scan": True, "mask": True}, (
        f"the event loop was blocked while these ran: {[s for s, ok in served.items() if not ok]}"
    )

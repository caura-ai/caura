"""``ingest_preview`` parses and chunks a document off the event loop.

Both steps are pure-Python CPU work. At ``DOC_HARD_TOKEN_LIMIT`` that is about
0.1s to parse a markdown document and about 0.8s to chunk one long paragraph,
measured locally. Run on the event loop, every other request the worker is
serving waits that long. They run in a worker thread instead, and the loop
keeps serving.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from core_api.schemas import IngestRequest
from core_api.services import ingest_service

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# Three sections: the intro under the H1, then one per H2.
DOC = (
    "# Runbook\n\nThe intro section explains the service.\n\n"
    "## Deploy\n\nThe deploy section says to run the installer.\n\n"
    "## Rollback\n\nThe rollback section says to restore the snapshot.\n"
)


async def test_the_loop_keeps_serving_while_a_document_is_parsed_and_chunked(
    monkeypatch,
):
    """Each stand-in asks the loop to run a callback, then waits for it.

    Off the loop the loop is free, so the callback runs at once. On the loop
    nothing else can run until the step returns, so the wait times out and the
    callback is reported as never served.
    """
    loop = asyncio.get_running_loop()
    served: dict[str, bool] = {}

    def _serving(step: str, real):
        def stand_in(arg):
            ran = threading.Event()
            loop.call_soon_threadsafe(ran.set)
            served[step] = ran.wait(timeout=2.0)
            return real(arg)

        return stand_in

    async def _config(tenant_id):
        return SimpleNamespace(enrichment_provider="openai", enrichment_enabled=True)

    async def _no_cache(*_args, **_kwargs):
        return []

    async def _chunk(text, focus=None, tenant_config=None, breadcrumb=None):
        return [{"content": f"A fact from: {text[:40]}", "suggested_type": "fact"}]

    monkeypatch.setattr(ingest_service, "resolve_config", _config)
    monkeypatch.setattr(ingest_service, "_find_prior_ingest_by_doc_hash", _no_cache)
    monkeypatch.setattr(ingest_service, "_chunk_content", _chunk)
    monkeypatch.setattr(
        ingest_service, "parse", _serving("parse", ingest_service.parse)
    )
    monkeypatch.setattr(
        ingest_service,
        "chunk_blocks",
        _serving("chunk_blocks", ingest_service.chunk_blocks),
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

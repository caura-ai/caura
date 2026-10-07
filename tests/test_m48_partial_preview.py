"""M-48: a preview that lost sections says so, and is never cached as whole.

A section whose extraction failed contributed ``[]`` with no marker: an outage
of every configured provider reached ``call_with_fallback``'s last resort,
``_fake_ingest``, and anything else was swallowed by ``_extract_section``. The
response still carried ``doc_hash``, so the commit stamped it, its parent
recorded ``errored=0`` and every later preview of the same document served the
truncated extraction from the cache, with no LLM call to recover it.

Now an outage raises out of ``_chunk_content`` (a ``RuntimeError``, which
auto-chunk already catches and falls back from, as it did on ``[]``), preview
counts failed sections in ``sections_failed``, a partial preview carries no
``doc_hash``, and a preview that lost every section is a 502. A deliberately
configured ``fake`` or ``none`` provider is not a failure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core_api.schemas import IngestRequest
from core_api.services import ingest_service

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# Three sections: the intro under the H1, then one per H2.
DOC = (
    "# Runbook\n\nThe intro section explains the service.\n\n"
    "## Deploy\n\nThe deploy section says to run the installer.\n\n"
    "## Rollback\n\nThe rollback section says to restore the snapshot.\n"
)


async def _outage(*, primary_provider_name, call_fn, fake_fn, **_kw):
    """``call_with_fallback`` after every provider failed: its last resort."""
    return fake_fn()


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_an_outage_is_not_an_empty_extraction(monkeypatch, provider):
    monkeypatch.setattr(ingest_service, "call_with_fallback", _outage)

    with pytest.raises(RuntimeError):
        await ingest_service._chunk_content(
            "text", tenant_config=SimpleNamespace(enrichment_provider=provider)
        )


@pytest.mark.parametrize("provider", ["fake", "none"])
async def test_a_chosen_stub_still_extracts_nothing(monkeypatch, provider):
    """The control: ``fake`` and ``none`` are the operator's choice, not a failure."""
    monkeypatch.setattr(ingest_service, "call_with_fallback", _outage)

    facts = await ingest_service._chunk_content(
        "text", tenant_config=SimpleNamespace(enrichment_provider=provider)
    )

    assert facts == []


async def _preview(monkeypatch, failing: set[str]):
    """Preview ``DOC``, failing every section whose text names one of ``failing``."""

    async def _config(tenant_id):
        return SimpleNamespace(enrichment_provider="openai", enrichment_enabled=True)

    async def _no_cache(*_args, **_kwargs):
        return []

    async def _chunk(text, focus=None, tenant_config=None, breadcrumb=None):
        if any(word in text for word in failing):
            raise RuntimeError("provider down")
        return [{"content": f"A fact from: {text[:40]}", "suggested_type": "fact"}]

    monkeypatch.setattr(ingest_service, "resolve_config", _config)
    monkeypatch.setattr(ingest_service, "_find_prior_ingest_by_doc_hash", _no_cache)
    monkeypatch.setattr(ingest_service, "_chunk_content", _chunk)
    return await ingest_service.ingest_preview(
        IngestRequest(tenant_id="t1", content=DOC)
    )


async def test_a_clean_preview_reports_no_failed_sections(monkeypatch):
    resp = await _preview(monkeypatch, failing=set())

    assert resp["sections"] == 3
    assert resp["sections_failed"] == 0
    assert resp["doc_hash"]
    assert len(resp["facts"]) == 3


async def test_a_partial_preview_counts_the_loss_and_cannot_be_cached(monkeypatch):
    resp = await _preview(monkeypatch, failing={"rollback"})

    assert resp["sections"] == 3
    assert resp["sections_failed"] == 1
    # The facts that did come back are still returned.
    assert len(resp["facts"]) == 2
    # No hash, so the commit stamps none and no later preview hits this run.
    assert resp["doc_hash"] is None


async def test_a_preview_that_lost_every_section_is_a_502(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await _preview(monkeypatch, failing={"intro", "deploy", "rollback"})

    assert exc.value.status_code == 502

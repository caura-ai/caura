"""09/02 M-44 — a partial ingest was cached as the finished one, permanently.

``ingest_commit`` tolerates partial failure. It counts ``created`` and
``errored`` and, when facts fail, logs a warning suggesting the operator wipe
the batch by ``ingest_run_id``. The rows that DID land still carry
``metadata["doc_hash"]``.

So the next preview of the same document found those rows, returned
``cached: True``, and served an incomplete extraction as the complete one — and
it could never recover, because the cache short-circuits before any LLM call.
Re-previewing, the obvious remedy, was precisely the thing that could not help.

The signal already existed and simply was not read: the parent Document records
``errored`` right next to ``doc_hash``. The cache-hit path now consults it.
"""

import inspect
from types import SimpleNamespace

import pytest

from core_api.services import ingest_service

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_tenant_config(monkeypatch):
    """Local copy of the ingest suites' fixture — enrichment off, fast writes,
    so preview exercises chunking rather than a provider."""

    async def _fake(tenant_id):
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    monkeypatch.setattr(ingest_service, "resolve_config", _fake)


# ── the completeness check ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_clean_prior_run_is_complete(monkeypatch):
    async def _doc(tenant_id, collection, doc_id, **kw):
        return {"data": {"errored": 0, "memory_count": 12}}

    monkeypatch.setattr(
        ingest_service,
        "get_storage_client",
        lambda: type("SC", (), {"get_document": staticmethod(_doc)})(),
    )
    assert await ingest_service._prior_ingest_was_complete("t1", "run-1") is True


@pytest.mark.asyncio
async def test_a_partial_prior_run_is_not_complete(monkeypatch):
    """The bug, stated directly: some facts failed, so what is cached is not
    the document."""

    async def _doc(tenant_id, collection, doc_id, **kw):
        return {"data": {"errored": 3, "memory_count": 9}}

    monkeypatch.setattr(
        ingest_service,
        "get_storage_client",
        lambda: type("SC", (), {"get_document": staticmethod(_doc)})(),
    )
    assert await ingest_service._prior_ingest_was_complete("t1", "run-1") is False


@pytest.mark.asyncio
async def test_a_missing_parent_is_not_complete(monkeypatch):
    """The parent write is best-effort, so absence means "cannot prove whole".

    Being wrong this way costs one extraction. Being wrong the other way leaves
    a document permanently missing facts, which is the failure this closes.
    """

    async def _doc(tenant_id, collection, doc_id, **kw):
        return None

    monkeypatch.setattr(
        ingest_service,
        "get_storage_client",
        lambda: type("SC", (), {"get_document": staticmethod(_doc)})(),
    )
    assert await ingest_service._prior_ingest_was_complete("t1", "run-1") is False


@pytest.mark.asyncio
async def test_an_absent_errored_field_is_not_complete(monkeypatch):
    """Parents written before ``errored`` existed cannot prove anything either."""

    async def _doc(tenant_id, collection, doc_id, **kw):
        return {"data": {"memory_count": 12}}

    monkeypatch.setattr(
        ingest_service,
        "get_storage_client",
        lambda: type("SC", (), {"get_document": staticmethod(_doc)})(),
    )
    assert await ingest_service._prior_ingest_was_complete("t1", "run-1") is False


@pytest.mark.asyncio
async def test_a_storage_failure_is_not_complete(monkeypatch):
    """And it must not raise — this runs on the preview path."""

    def _boom():
        raise RuntimeError("storage down")

    monkeypatch.setattr(ingest_service, "get_storage_client", _boom)
    assert await ingest_service._prior_ingest_was_complete("t1", "run-1") is False


# ── the gate actually guards the cache ────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_refuses_a_partial_cache_and_re_extracts(
    monkeypatch, fake_tenant_config
):
    """End to end: prior rows exist for this doc_hash, but the run that wrote
    them was partial — so preview must re-extract rather than serve them."""
    prior = {
        "content": "partial fact",
        "memory_type": "fact",
        "source_uri": "text-input",
        "run_id": "run-partial",
        "metadata_": {"source": "ingest", "doc_hash": "h"},
    }

    async def _lookup(tenant_id, doc_hash):
        return [prior]

    async def _incomplete(tenant_id, run_id):
        return False

    chunked = False

    async def _chunk(text, focus=None, tenant_config=None, breadcrumb=None):
        nonlocal chunked
        chunked = True
        return [{"content": "freshly extracted", "suggested_type": "fact"}]

    monkeypatch.setattr(ingest_service, "_find_prior_ingest_by_doc_hash", _lookup)
    monkeypatch.setattr(ingest_service, "_prior_ingest_was_complete", _incomplete)
    monkeypatch.setattr(ingest_service, "_chunk_content", _chunk)

    from core_api.schemas import IngestRequest

    resp = await ingest_service.ingest_preview(
        request=IngestRequest(tenant_id="t1", content="a document worth re-extracting")
    )

    assert resp.get("cached") is not True, (
        "a partial prior run must not be served as cached"
    )
    assert chunked, "refusing the cache has to actually re-extract"


def test_the_gate_runs_before_the_cache_is_served():
    """Ordering. A check placed after the cached response is built would return
    the partial result and then think about it."""
    src = inspect.getsource(ingest_service.ingest_preview)
    assert src.index("_prior_ingest_was_complete") < src.index('"cached": True')

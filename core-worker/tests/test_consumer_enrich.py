"""Tests for ``handle_enrich_request`` (CAURA-595).

Covers payload validation, tenant-config reconstruction, ORM /
metadata-patch field split, and the same poison-message + transient-vs-
permanent-failure guards as the embed-request consumer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

import core_worker.consumer as consumer
from common.enrichment import EnrichmentResult
from common.events.base import Event


def _make_event(payload: dict | None = None) -> Event:
    p = payload or {
        "memory_id": str(uuid4()),
        "tenant_id": "tenant-A",
        "content": "we decided to go with postgres",
        "enrichment_provider": "openai",
        "openai_api_key": "sk-tenant",
    }
    return Event(
        event_type="memclaw.memory.enrich-requested",
        tenant_id=p.get("tenant_id"),
        payload=p,
    )


@pytest.fixture
def mock_storage_client():
    client = MagicMock(spec=httpx.AsyncClient)
    return lambda: client


@pytest.fixture
def stub_enrich(monkeypatch):
    """Replace ``enrich_memory`` with an AsyncMock returning a deterministic result."""

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        return EnrichmentResult(
            memory_type="decision",
            weight=0.85,
            title="Postgres over Mongo",
            summary="Decided to use PostgreSQL.",
            tags=["db", "decision"],
            status="active",
            ts_valid_start=None,
            ts_valid_end=None,
            contains_pii=False,
            pii_types=[],
            retrieval_hint="database technology decision",
            llm_ms=42,
        )

    spy = AsyncMock(side_effect=_enrich)
    monkeypatch.setattr(consumer, "enrich_memory", spy)
    return spy


@pytest.fixture(autouse=True)
def stub_enriched_publish(monkeypatch):
    """Default-stub the back-channel publish so tests don't hit the real bus.

    Tests that need to assert on the publish (e.g.
    ``test_publishes_enriched_after_patch_succeeds``) override this with
    their own ``AsyncMock`` via ``monkeypatch``.
    """
    monkeypatch.setattr(consumer, "publish_memory_enriched", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_happy_path_runs_enricher_and_patches(monkeypatch, mock_storage_client, stub_enrich):
    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    memory_id = uuid4()
    event = _make_event(
        {
            "memory_id": str(memory_id),
            "tenant_id": "tenant-A",
            "content": "we decided to go with postgres",
            "enrichment_provider": "openai",
            "openai_api_key": "sk-tenant",
        }
    )

    await consumer.handle_enrich_request(event)

    stub_enrich.assert_awaited_once()
    patch_call.assert_awaited_once()
    call = patch_call.await_args.kwargs
    assert call["memory_id"] == memory_id
    assert call["tenant_id"] == "tenant-A"

    fields = call["fields"]
    # ORM-column fields go top-level
    assert fields["memory_type"] == "decision"
    assert fields["weight"] == 0.85
    assert fields["title"] == "Postgres over Mongo"
    assert fields["status"] == "active"
    # ts_valid_* are explicitly preserved as ``None`` even when the
    # enrichment didn't produce them, so a re-delivery / second LLM
    # run can null-clear stale dates a previous run wrote. (The
    # ``exclude_none=True`` dump would otherwise drop them.)
    assert fields["ts_valid_start"] is None
    assert fields["ts_valid_end"] is None

    # Metadata fields go under metadata_patch
    mp = fields["metadata_patch"]
    # The hot-path writer set ``enrichment_pending=true`` when deferring;
    # the worker clears it on every successful PATCH.
    assert mp["enrichment_pending"] is False
    assert mp["summary"] == "Decided to use PostgreSQL."
    assert mp["tags"] == ["db", "decision"]
    assert mp["retrieval_hint"] == "database technology decision"
    assert mp["llm_ms"] == 42
    # PII fields always pass through (non-None) so a redelivery can
    # clear a stale ``contains_pii=True`` left by an earlier run.
    assert mp["contains_pii"] is False
    assert mp["pii_types"] == []


@pytest.mark.asyncio
async def test_tenant_config_reconstructs_from_payload(monkeypatch, mock_storage_client, stub_enrich):
    """The duck-typed tenant_config carries the publisher's resolved values."""
    consumer.configure(mock_storage_client)
    monkeypatch.setattr(consumer, "update_memory_enrichment", AsyncMock())

    captured: dict = {}

    async def _capture(content, tenant_config=None, *, reference_datetime=None):
        captured["tenant_config"] = tenant_config
        captured["reference_datetime"] = reference_datetime
        return EnrichmentResult()

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_capture))

    event = _make_event(
        {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "anything",
            "reference_datetime": "2026-04-25T12:00:00+00:00",
            "enrichment_provider": "gemini",
            "enrichment_model": "gemini-2.0-flash",
            "gemini_api_key": "AIza-tenant",
            "fallback_provider": "openai",
            "fallback_model": "gpt-4o-mini",
        }
    )

    await consumer.handle_enrich_request(event)

    tc = captured["tenant_config"]
    assert tc.enrichment_provider == "gemini"
    assert tc.enrichment_model == "gemini-2.0-flash"
    assert tc.gemini_api_key == "AIza-tenant"
    # Fallback resolution preserved as a callable that returns the publisher's tuple.
    assert tc.resolve_fallback() == ("openai", "gpt-4o-mini")
    # reference_datetime parsed back to a datetime
    assert captured["reference_datetime"].year == 2026


@pytest.mark.asyncio
async def test_tags_can_be_cleared_on_redelivery(monkeypatch, mock_storage_client):
    """``tags=[]`` from a real LLM run is intentional ("no tags") and must
    overwrite a prior non-empty list. The enrichment prompt instructs
    the LLM to populate 2-6 tags, so an empty list is a deliberate
    signal — not a heuristic-fallback artefact to filter out."""
    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        # Real LLM run (llm_ms > 0) that returned no tags — must
        # overwrite any stale list. Heuristic-fallback case is locked
        # by ``test_always_write_metadata_NOT_cleared_on_heuristic_redelivery``.
        return EnrichmentResult(memory_type="fact", title="x", tags=[], llm_ms=42)

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    await consumer.handle_enrich_request(_make_event())

    fields = patch_call.await_args.kwargs["fields"]
    # tags=[] is in the metadata_patch — will overwrite any stale list.
    assert fields["metadata_patch"]["tags"] == []


@pytest.mark.asyncio
async def test_ts_valid_can_be_null_cleared_on_real_llm_redelivery(monkeypatch, mock_storage_client):
    """A re-enrichment from a REAL LLM run (``llm_ms > 0``) that
    produces no temporal bounds MUST be able to clear ``ts_valid_*``
    previously written — the PATCH carries explicit ``None`` for those
    columns. Distinguished from the heuristic-fallback case below by
    the ``llm_ms`` proxy."""
    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        # Real LLM call (any llm_ms > 0) with no inferred dates —
        # treat absence as "no bounds" and clear stale.
        return EnrichmentResult(memory_type="fact", title="x", llm_ms=42)

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    await consumer.handle_enrich_request(_make_event())

    fields = patch_call.await_args.kwargs["fields"]
    assert fields["ts_valid_start"] is None
    assert fields["ts_valid_end"] is None


@pytest.mark.asyncio
async def test_always_write_metadata_NOT_cleared_on_heuristic_redelivery(monkeypatch, mock_storage_client):
    """A redelivery that hits ``fake_enrich`` (LLM outage) MUST NOT
    clobber real LLM-produced metadata a prior successful run wrote.
    ``fake_enrich`` returns ``contains_pii=False``, ``pii_types=[]``,
    ``tags=[]``, ``retrieval_hint=""`` as Pydantic defaults plus a
    ``summary=content[:200]`` truncation — all are non-None and most
    are non-empty, so without the ``llm_ms > 0`` guard the always-
    write logic would silently downgrade prior good values to
    heuristic-quality defaults."""
    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        # Heuristic-fallback shape: llm_ms=0, plus the
        # content-truncation summary that ``fake_enrich`` produces.
        # Reproduces the real-world clobbering risk for the
        # ``summary`` field.
        return EnrichmentResult(
            memory_type="fact",
            title="x",
            summary="some content truncation from fake_enrich",
            llm_ms=0,
        )

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    await consumer.handle_enrich_request(_make_event())

    fields = patch_call.await_args.kwargs["fields"]
    mp = fields.get("metadata_patch", {})
    # None of the always-write metadata fields appear in the PATCH —
    # prior values survive the heuristic redelivery.
    assert "contains_pii" not in mp
    assert "pii_types" not in mp
    assert "tags" not in mp
    assert "retrieval_hint" not in mp
    assert "summary" not in mp  # heuristic content-truncation must not clobber LLM summary


@pytest.mark.asyncio
async def test_ts_valid_NOT_cleared_on_heuristic_fallback_redelivery(monkeypatch, mock_storage_client):
    """A redelivery that hits the keyword heuristic (``llm_ms == 0``,
    typically because the LLM was down) MUST NOT clobber temporal
    bounds a prior successful LLM run wrote. The heuristic never
    infers dates so its absence-of-ts_valid is ambiguous — preserve
    whatever's in storage."""
    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        # Heuristic-fallback shape: llm_ms=0, no temporal data.
        return EnrichmentResult(memory_type="fact", title="x", llm_ms=0)

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    await consumer.handle_enrich_request(_make_event())

    fields = patch_call.await_args.kwargs["fields"]
    # Neither temporal column is in the PATCH — prior values survive.
    assert "ts_valid_start" not in fields
    assert "ts_valid_end" not in fields


@pytest.mark.asyncio
async def test_ts_valid_skipped_when_agent_provided(monkeypatch, mock_storage_client):
    """``agent_provided_fields`` still wins — agent-set ts_valid_* must
    survive re-enrichment regardless of the null-clearing behaviour."""
    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        return EnrichmentResult(memory_type="fact")

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    event = _make_event(
        {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "anything",
            "agent_provided_fields": ["ts_valid_start", "ts_valid_end"],
        }
    )
    await consumer.handle_enrich_request(event)

    fields = patch_call.await_args.kwargs["fields"]
    # Skip-list wins: agent-set columns are NOT in the PATCH at all.
    assert "ts_valid_start" not in fields
    assert "ts_valid_end" not in fields


@pytest.mark.asyncio
async def test_agent_provided_fields_skip_orm_overwrite(monkeypatch, mock_storage_client):
    """Fields the agent set at write time MUST NOT be PATCHed by the worker.

    Critical: ``EnrichmentResult`` has Pydantic defaults
    (``memory_type="fact"``, ``weight=0.7``, ``status="active"``) that
    survive ``model_dump(exclude_none=True)``. Without this gate the
    worker would silently downgrade an agent-provided ``weight=0.9``
    to the schema default on every redelivery.
    """
    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        return EnrichmentResult(
            memory_type="decision",
            weight=0.85,
            title="x",
            summary="y",
            tags=["t"],
            llm_ms=42,
        )

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    event = _make_event(
        {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "anything",
            "agent_provided_fields": ["weight", "memory_type"],
        }
    )

    await consumer.handle_enrich_request(event)

    fields = patch_call.await_args.kwargs["fields"]
    # Agent-set columns are NOT in the PATCH
    assert "memory_type" not in fields
    assert "weight" not in fields
    # Other fields still flow
    assert fields["title"] == "x"
    assert fields["metadata_patch"]["summary"] == "y"
    assert fields["metadata_patch"]["tags"] == ["t"]


@pytest.mark.asyncio
async def test_agent_provided_metadata_field_skipped(monkeypatch, mock_storage_client):
    """``agent_provided_fields`` excludes metadata-side fields too."""
    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        return EnrichmentResult(
            memory_type="decision",
            title="x",
            summary="enricher-summary",
            tags=["a"],
            llm_ms=42,
        )

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    event = _make_event(
        {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "anything",
            "agent_provided_fields": ["summary"],
        }
    )

    await consumer.handle_enrich_request(event)

    fields = patch_call.await_args.kwargs["fields"]
    # Summary skipped — but tags still flowed through
    mp = fields.get("metadata_patch", {})
    assert "summary" not in mp
    assert mp.get("tags") == ["a"]


@pytest.mark.asyncio
async def test_enrichment_pending_cleared_even_with_heuristic_fallback(monkeypatch, mock_storage_client):
    """Even when the LLM path collapsed to ``fake_enrich`` and produced
    no real metadata, the worker still PATCHes ``enrichment_pending=False``
    so a read-after-success doesn't leave a stale "still pending" hint
    in metadata. Without this seed the patch dict would contain only
    column-level fields (memory_type, weight) and ``metadata_patch``
    would be entirely absent, preserving the stale flag forever."""
    from common.enrichment.schema import EnrichmentResult

    consumer.configure(mock_storage_client)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch_call)

    # Heuristic fallback shape: zero llm_ms so the always-write metadata
    # fields (tags, summary, retrieval_hint, contains_pii, pii_types)
    # all skip per the ``llm_ms > 0`` guard. Empty metadata_patch is
    # exactly the regression scenario we're guarding against.
    heuristic = EnrichmentResult(
        memory_type="generic",
        weight=0.5,
        title="",
        summary="",
        tags=[],
        contains_pii=False,
        pii_types=[],
        retrieval_hint="",
        llm_ms=0,
    )

    async def fake_enrich(*_a, **_kw):
        return heuristic

    monkeypatch.setattr("core_worker.consumer.enrich_memory", fake_enrich)

    event = _make_event(
        {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "x",
        }
    )

    await consumer.handle_enrich_request(event)

    fields = patch_call.await_args.kwargs["fields"]
    mp = fields["metadata_patch"]
    assert mp == {"enrichment_pending": False}, "heuristic fallback PATCH must still clear enrichment_pending"


@pytest.mark.asyncio
async def test_no_provider_in_payload_passes_none(monkeypatch, mock_storage_client, stub_enrich):
    """Without ``enrichment_provider`` the worker passes tenant_config=None."""
    consumer.configure(mock_storage_client)
    monkeypatch.setattr(consumer, "update_memory_enrichment", AsyncMock())

    captured: dict = {}

    async def _capture(content, tenant_config=None, *, reference_datetime=None):
        captured["tenant_config"] = tenant_config
        return EnrichmentResult()

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_capture))

    event = _make_event(
        {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "anything",
        }
    )

    await consumer.handle_enrich_request(event)

    assert captured["tenant_config"] is None


@pytest.mark.asyncio
async def test_validation_error_drops_silently(mock_storage_client, caplog):
    consumer.configure(mock_storage_client)

    # Missing required fields.
    event = Event(event_type="memclaw.memory.enrich-requested", payload={})

    with caplog.at_level("ERROR"):
        await consumer.handle_enrich_request(event)

    assert any(getattr(rec, "dropped", False) is True for rec in caplog.records), (
        "expected an alert-hook log record (dropped=True)"
    )


@pytest.mark.asyncio
async def test_unconfigured_drops_silently(monkeypatch, caplog):
    """Pre-``configure()`` event is dropped, doesn't raise."""
    monkeypatch.setattr(consumer, "_storage_client_factory", None)
    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock())

    with caplog.at_level("ERROR"):
        await consumer.handle_enrich_request(_make_event())

    assert any(getattr(rec, "dropped", False) is True for rec in caplog.records)


@pytest.mark.asyncio
async def test_storage_404_acks_silently(caplog):
    from core_worker.clients.storage_client import update_memory_enrichment

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=MagicMock(status_code=404))

    with caplog.at_level("WARNING"):
        await update_memory_enrichment(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            fields={"memory_type": "decision"},
        )

    assert any("not found in storage" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_storage_422_acks_silently(caplog):
    from core_worker.clients.storage_client import update_memory_enrichment

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=MagicMock(status_code=422))

    with caplog.at_level("WARNING"):
        await update_memory_enrichment(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            fields={"memory_type": "decision"},
        )

    assert any("422" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_storage_500_propagates():
    from core_worker.clients.storage_client import update_memory_enrichment

    response = MagicMock(status_code=500)
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=response)
    )
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        await update_memory_enrichment(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            fields={"memory_type": "decision"},
        )


@pytest.mark.asyncio
async def test_publishes_enriched_after_patch_succeeds(monkeypatch, mock_storage_client):
    """After a successful PATCH, the worker emits ``Topics.Memory.ENRICHED``
    so core-api (or any other subscriber) can react — primarily to
    schedule a hint-driven re-embed when the LLM produced one.
    """
    consumer.configure(mock_storage_client)
    monkeypatch.setattr(consumer, "update_memory_enrichment", AsyncMock(return_value=None))

    publish_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "publish_memory_enriched", publish_spy)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        return EnrichmentResult(
            memory_type="decision",
            title="x",
            retrieval_hint="business milestone: chose Postgres",
        )

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    memory_id = uuid4()
    event = _make_event(
        {
            "memory_id": str(memory_id),
            "tenant_id": "tenant-A",
            "content": "we decided to go with postgres",
        }
    )

    await consumer.handle_enrich_request(event)

    publish_spy.assert_awaited_once()
    kwargs = publish_spy.await_args.kwargs
    assert kwargs["memory_id"] == memory_id
    assert kwargs["tenant_id"] == "tenant-A"
    assert kwargs["content"] == "we decided to go with postgres"
    assert kwargs["retrieval_hint"] == "business milestone: chose Postgres"


@pytest.mark.asyncio
async def test_publishes_enriched_with_empty_hint_when_none_produced(monkeypatch, mock_storage_client):
    """Heuristic fallback / content-already-aligned enrichments produce no
    ``retrieval_hint`` — back-channel still fires (subscribers may
    care about the bare event), with the hint as an empty string."""
    consumer.configure(mock_storage_client)
    monkeypatch.setattr(consumer, "update_memory_enrichment", AsyncMock(return_value=None))

    publish_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "publish_memory_enriched", publish_spy)

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        return EnrichmentResult(memory_type="fact")  # no retrieval_hint

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    await consumer.handle_enrich_request(_make_event())

    publish_spy.assert_awaited_once()
    assert publish_spy.await_args.kwargs["retrieval_hint"] == ""


@pytest.mark.asyncio
async def test_back_channel_publish_failure_does_not_nack(monkeypatch, mock_storage_client, caplog):
    """Best-effort: a publish failure on the back-channel must NOT raise out
    of ``handle_enrich_request`` — the upstream PATCH already succeeded
    and the enrichment is durable in storage. Pub/Sub would otherwise
    redeliver the input event and re-PATCH the same fields wastefully.
    """
    consumer.configure(mock_storage_client)
    monkeypatch.setattr(consumer, "update_memory_enrichment", AsyncMock(return_value=None))

    monkeypatch.setattr(
        consumer,
        "publish_memory_enriched",
        AsyncMock(side_effect=RuntimeError("bus down")),
    )

    async def _enrich(content, tenant_config=None, *, reference_datetime=None):
        return EnrichmentResult(memory_type="fact", retrieval_hint="hint")

    monkeypatch.setattr(consumer, "enrich_memory", AsyncMock(side_effect=_enrich))

    with caplog.at_level("ERROR"):
        # Must not raise.
        await consumer.handle_enrich_request(_make_event())

    assert any("back-channel publish failed" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_empty_fields_skip_patch(monkeypatch, caplog):
    """Empty fields dict short-circuits — no PATCH call to storage."""
    from core_worker.clients.storage_client import update_memory_enrichment

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock()

    with caplog.at_level("DEBUG"):
        await update_memory_enrichment(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            fields={},
        )

    client.patch.assert_not_called()

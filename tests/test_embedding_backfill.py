"""Unit tests for the event-driven embedding backfill task.

The task lives at ``core-worker/src/core_worker/backfill.py`` and drives
the existing ``handle_embed_request`` consumer by publishing one
``EMBED_REQUESTED`` event per memory whose ``embedding IS NULL``.

Mocks the storage-client iterator + the per-row ``get_memory`` fetch
+ the embed-request publisher. No real DB or event bus. Integration
coverage (against staging Postgres + a Pub/Sub emulator) is covered
by the staging cutover runbook (Spec E), not this PR.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core_worker.backfill import run_embedding_backfill
from core_worker.clients.storage_client import NullEmbeddingRow


def _row() -> NullEmbeddingRow:
    return NullEmbeddingRow(memory_id=uuid.uuid4(), tenant_id="tenant-A")


def _make_get_memory(
    content: str = "hello", content_hash: str | None = None
) -> AsyncMock:
    """Build an ``AsyncMock`` for ``get_memory`` that returns a fake
    memory dict matching the storage API's shape."""
    return AsyncMock(return_value={"content": content, "content_hash": content_hash})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_publishes_one_event_per_null_row() -> None:
    """Happy path: every NULL row → one ``get_memory`` fetch + one
    ``publish_memory_embed_request`` call."""
    rows = [_row() for _ in range(7)]

    async def _fake_iter(_storage, **_kw):
        # Two pages: 5 + 2.
        yield rows[:5]
        yield rows[5:]

    publish = AsyncMock()
    get_memory = _make_get_memory()
    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        report = await run_embedding_backfill(
            tenant_id="t-1", batch_size=5, max_inflight=2
        )

    assert report.scanned == 7
    assert report.published == 7
    assert report.skipped_missing == 0
    assert get_memory.await_count == 7
    assert publish.await_count == 7
    sample = publish.await_args_list[0].kwargs
    assert {"memory_id", "tenant_id", "content", "content_hash"} <= set(sample)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_dry_run_fetches_content_but_does_not_publish() -> None:
    """``--dry-run`` still does the per-row content fetch (so the report
    accurately accounts for soft-deleted rows) but skips the publish."""
    rows = [_row() for _ in range(3)]

    async def _fake_iter(_storage, **_kw):
        yield rows

    publish = AsyncMock()
    get_memory = _make_get_memory()
    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        report = await run_embedding_backfill(tenant_id="t-1", dry_run=True)

    assert report.scanned == 3
    assert report.published == 3  # counted as "would have"
    assert get_memory.await_count == 3
    assert publish.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_no_null_rows_returns_zero_report() -> None:
    """No NULL rows → empty report, no fetches, no publishes, clean exit."""

    async def _fake_iter(_storage, **_kw):
        if False:  # generator with no yields
            yield

    publish = AsyncMock()
    get_memory = _make_get_memory()
    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        report = await run_embedding_backfill(tenant_id="t-1")

    assert report.scanned == 0
    assert report.published == 0
    assert get_memory.await_count == 0
    assert publish.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_passes_tenant_filter_to_iterator() -> None:
    """``tenant_id`` and ``batch_size`` reach the storage iterator."""
    captured: dict = {}

    async def _fake_iter(_storage, **kw):
        captured.update(kw)
        if False:
            yield

    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", AsyncMock()),
        patch("core_worker.backfill.get_memory", _make_get_memory()),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        await run_embedding_backfill(tenant_id="tenant-A", batch_size=42)

    assert captured["tenant_id"] == "tenant-A"
    assert captured["batch_size"] == 42


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_publishes_full_embed_request_payload() -> None:
    """Each publish call receives content + hash + tenant — fetched
    per-row from ``get_memory`` after the listing endpoint hands the
    worker the id. Guards against regressing to a partial payload that
    the consumer's Pydantic model would reject and burn the DLQ budget on."""
    row = NullEmbeddingRow(memory_id=uuid.uuid4(), tenant_id="tenant-X")

    async def _fake_iter(_storage, **_kw):
        yield [row]

    publish = AsyncMock()
    get_memory = _make_get_memory(content="some memory body", content_hash="hash-abc")
    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        await run_embedding_backfill(tenant_id="t-1")

    get_memory.assert_awaited_once_with(
        get_memory.await_args.args[0],  # the storage client
        memory_id=row.memory_id,
        tenant_id="tenant-X",
    )
    publish.assert_awaited_once_with(
        memory_id=row.memory_id,
        content="some memory body",
        tenant_id="tenant-X",
        content_hash="hash-abc",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_skips_404_between_listing_and_fetch() -> None:
    """A row that the listing endpoint reported but ``get_memory``
    can't find (soft-/hard-deleted in the gap) is counted as
    ``skipped_missing`` rather than failing the whole backfill."""
    rows = [_row() for _ in range(3)]

    async def _fake_iter(_storage, **_kw):
        yield rows

    publish = AsyncMock()

    # Build a 404 HTTPStatusError for one of the three rows.
    not_found = httpx.HTTPStatusError(
        "404",
        request=httpx.Request("GET", "http://x/memories/y"),
        response=httpx.Response(status_code=404),
    )
    get_memory = AsyncMock(
        side_effect=[
            {"content": "first", "content_hash": None},
            not_found,
            {"content": "third", "content_hash": None},
        ]
    )

    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        report = await run_embedding_backfill(tenant_id="t-1")

    assert report.scanned == 3
    assert report.published == 2
    assert report.skipped_missing == 1
    assert publish.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_propagates_non_404_http_errors() -> None:
    """A 500 from the storage API is NOT silently absorbed — the
    backfill aborts so the operator notices instead of a silent
    long-running no-op."""
    rows = [_row()]

    async def _fake_iter(_storage, **_kw):
        yield rows

    server_error = httpx.HTTPStatusError(
        "500",
        request=httpx.Request("GET", "http://x/memories/y"),
        response=httpx.Response(status_code=500),
    )
    get_memory = AsyncMock(side_effect=server_error)

    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", AsyncMock()),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await run_embedding_backfill(tenant_id="t-1")

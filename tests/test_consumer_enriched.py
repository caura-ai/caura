"""Tests for ``core_api.consumer.handle_memory_enriched`` (CAURA-595 Phase 5).

This is the core-api side of the ``Topics.Memory.ENRICHED`` back-channel:
the worker publishes after a successful enrichment PATCH; core-api
subscribes here and triggers contradiction detection on the async
write path. Atomic-fact fan-out is intentionally out of scope (see
the module docstring for the Phase 5b gap).

Coverage map
* Happy path — embedding present → detect_contradictions_async called
  with the right shape (memory_id / tenant_id / fleet_id / content /
  embedding).
* Embedding not yet landed (race against embed worker) — handler logs
  "deferred" and ack-completes without invoking detection.
* Storage 404 — handler ack-drops cleanly without invoking detection.
* Malformed payload — Pydantic ValidationError → ack-drop, dropped=True
  log entry (poison-message guard).
* register_consumers wires the right topic + handler.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.events.base import Event
from common.events.topics import Topics
from core_api import consumer

pytestmark = pytest.mark.asyncio


def _make_event(payload: dict | None = None) -> Event:
    p = payload or {
        "memory_id": str(uuid.uuid4()),
        "tenant_id": "tenant-A",
        "content": "hello world",
        "retrieval_hint": "",
    }
    return Event(
        event_type=Topics.Memory.ENRICHED,
        tenant_id=p.get("tenant_id"),
        payload=p,
    )


@pytest.fixture
def mock_storage_client(monkeypatch):
    """Patch ``core_api.consumer.get_storage_client`` (the binding the
    consumer module captured at import time). Patching the source
    module instead would miss this binding and let the real httpx
    client through."""
    sc = MagicMock()
    sc.get_memory = AsyncMock()
    monkeypatch.setattr("core_api.consumer.get_storage_client", lambda: sc)
    return sc


@pytest.fixture
def mock_detect(monkeypatch):
    """Patch ``core_api.consumer.detect_contradictions_async`` (the
    consumer's bound reference, not the source module's). Same
    reasoning as ``mock_storage_client``."""
    fn = AsyncMock(return_value=None)
    monkeypatch.setattr("core_api.consumer.detect_contradictions_async", fn)
    return fn


# ── Happy path ────────────────────────────────────────────────────────


async def test_handle_memory_enriched_invokes_contradiction_detection(
    mock_storage_client, mock_detect
) -> None:
    """Embedding present → detect_contradictions_async called with the
    fleet_id from storage and the embedding vector inline."""
    memory_id = uuid.uuid4()
    mock_storage_client.get_memory.return_value = {
        "id": str(memory_id),
        "tenant_id": "tenant-A",
        "fleet_id": "fleet-X",
        "embedding": [0.1] * 1536,
    }

    event = _make_event(
        {
            "memory_id": str(memory_id),
            "tenant_id": "tenant-A",
            "content": "we decided to use postgres over mongo",
            "retrieval_hint": "database technology decision",
        }
    )

    await consumer.handle_memory_enriched(event)

    mock_storage_client.get_memory.assert_awaited_once_with(str(memory_id))
    # ``new_memory`` is passed kwarg-only — threading the row through
    # eliminates the redundant second ``get_memory`` inside
    # ``detect_contradictions_async``.
    mock_detect.assert_awaited_once_with(
        memory_id,
        "tenant-A",
        "fleet-X",
        "we decided to use postgres over mongo",
        [0.1] * 1536,
        new_memory=mock_storage_client.get_memory.return_value,
    )


async def test_handle_memory_enriched_passes_none_fleet_id(
    mock_storage_client, mock_detect
) -> None:
    """Single-tenant / no-fleet rows (``fleet_id is None``) must
    propagate as ``None`` to detect_contradictions_async — the function
    accepts ``str | None`` and uses None to mean tenant-wide search."""
    memory_id = uuid.uuid4()
    mock_storage_client.get_memory.return_value = {
        "id": str(memory_id),
        "fleet_id": None,
        "embedding": [0.5] * 8,
    }

    await consumer.handle_memory_enriched(
        _make_event(
            {"memory_id": str(memory_id), "tenant_id": "tenant-A", "content": "x"}
        )
    )

    fleet_arg = mock_detect.await_args.args[2]
    assert fleet_arg is None


# ── Embedding not yet landed (race against embed worker) ──────────────


async def test_handle_memory_enriched_defers_when_embedding_missing(
    mock_storage_client, mock_detect, caplog
) -> None:
    """Embed worker hasn't completed → embedding is NULL → log INFO and
    skip detection. Phase 5a accepts this; Phase 5b will close the gap
    via a parallel EMBEDDED-side consumer."""
    mock_storage_client.get_memory.return_value = {
        "id": "doesnt-matter",
        "fleet_id": None,
        "embedding": None,  # the race state we're asserting on
    }

    with caplog.at_level(logging.INFO, logger="core_api.consumer"):
        await consumer.handle_memory_enriched(_make_event())

    mock_detect.assert_not_called()
    assert any(
        "embedding not yet present" in rec.getMessage() for rec in caplog.records
    )


async def test_handle_memory_enriched_defers_on_empty_embedding(
    mock_storage_client, mock_detect
) -> None:
    """``[]`` falsy embedding must defer the same way as ``None``.
    Defends against a future storage shape that might emit an empty
    list instead of NULL."""
    mock_storage_client.get_memory.return_value = {
        "id": "any",
        "fleet_id": None,
        "embedding": [],
    }

    await consumer.handle_memory_enriched(_make_event())

    mock_detect.assert_not_called()


# ── Storage 404 (memory deleted between worker PATCH + this handler) ──


async def test_handle_memory_enriched_ack_drops_on_missing_memory(
    mock_storage_client, mock_detect, caplog
) -> None:
    """Memory deleted between worker PATCH and consumer dispatch → ack-
    drop with INFO. Common-enough race not to nack-loop on."""
    mock_storage_client.get_memory.return_value = None

    with caplog.at_level(logging.INFO, logger="core_api.consumer"):
        await consumer.handle_memory_enriched(_make_event())

    mock_detect.assert_not_called()
    assert any("target row missing" in rec.getMessage() for rec in caplog.records)


# ── Malformed payload → ack-drop with dropped=True ────────────────────


async def test_handle_memory_enriched_drops_malformed_payload(
    mock_storage_client, mock_detect, caplog
) -> None:
    """Schema-drift / poison message → ack-drop with the standard
    ``dropped=True`` log marker. Storage must not even be queried —
    we don't want to spend a roundtrip per redelivery on bad shape."""
    bad_event = Event(
        event_type=Topics.Memory.ENRICHED,
        tenant_id="tenant-A",
        # ``memory_id`` is required; missing it triggers ValidationError
        payload={"tenant_id": "tenant-A", "content": "x"},
    )

    with caplog.at_level(logging.ERROR, logger="core_api.consumer"):
        await consumer.handle_memory_enriched(bad_event)

    mock_storage_client.get_memory.assert_not_called()
    mock_detect.assert_not_called()
    assert any(rec.__dict__.get("dropped") is True for rec in caplog.records), (
        "expected a log record with dropped=True for the poison message"
    )


async def test_handle_memory_enriched_drops_non_mapping_payload(
    mock_storage_client, mock_detect, caplog
) -> None:
    """``event.payload`` not being a dict at all (a producer-side bug
    that emits a list / scalar / None) raises ``TypeError`` from
    ``**event.payload`` before Pydantic ever sees it. Must ack-drop
    the same way as a ``ValidationError`` rather than nack-loop on
    a poison message that will never deserialize."""
    # ``Event.payload`` is typed ``dict`` in Pydantic, so we can't
    # construct one with a list directly. Build a valid event and
    # mutate the attribute to side-step the model's own validation —
    # mirrors the failure shape we'd see if the publisher serialized
    # something weird and the broker round-tripped it as JSON.
    bad_event = Event(
        event_type=Topics.Memory.ENRICHED,
        tenant_id="tenant-A",
        payload={
            "memory_id": str(uuid.uuid4()),
            "tenant_id": "tenant-A",
            "content": "x",
        },
    )
    object.__setattr__(bad_event, "payload", [1, 2, 3])  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR, logger="core_api.consumer"):
        await consumer.handle_memory_enriched(bad_event)

    mock_storage_client.get_memory.assert_not_called()
    mock_detect.assert_not_called()
    assert any(rec.__dict__.get("dropped") is True for rec in caplog.records), (
        "expected a log record with dropped=True for the non-mapping payload"
    )


# ── register_consumers wires the right topic ──────────────────────────


async def test_register_consumers_subscribes_to_both_topics() -> None:
    """``register_consumers()`` MUST attach the right handler to each
    back-channel topic — a typo on either side would silently orphan
    the handler.

    Two subscriptions wired: ``ENRICHED`` (CAURA-595) and ``EMBEDDED``
    (the symmetric path that fires contradiction detection on the
    embed-after-enrich ordering — the only case under
    ``EMBED_ON_HOT_PATH=false``).
    """
    fake_bus = MagicMock()
    with patch("core_api.consumer.get_event_bus", return_value=fake_bus):
        consumer.register_consumers()

    assert fake_bus.subscribe.call_count == 2
    fake_bus.subscribe.assert_any_call(
        Topics.Memory.ENRICHED, consumer.handle_memory_enriched
    )
    fake_bus.subscribe.assert_any_call(
        Topics.Memory.EMBEDDED, consumer.handle_memory_embedded
    )

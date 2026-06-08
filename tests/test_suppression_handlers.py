"""Unit tests for the shared suppression consumer (CAURA-694).

Lives in ``common/events/suppression_handlers.py``; both core-worker
(SaaS) and any future OSS-standalone subscriber register the same code.
Exercise:

  - happy path: each tenant in the payload is upserted with the
    declared action
  - malformed payload (no ``action``) is dropped silently (no raise →
    Pub/Sub will ack and not redeliver)
  - partial failure (adapter raises on one tenant) re-raises so
    Pub/Sub redelivers
  - empty tenant_ids list is a well-formed no-op
  - correlation_id rides through to ``updated_by``; falls back to
    ``"core-worker"`` when absent
"""

from __future__ import annotations

import pytest

from common.events.base import Event
from common.events.suppression_handlers import (
    SuppressionStorageAdapter,
    _handle_suppression_changed,
)


class _FakeAdapter(SuppressionStorageAdapter):
    def __init__(self, *, raise_on_tenant: str | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self._raise_on_tenant = raise_on_tenant

    async def set_tenant_suppression(
        self, *, tenant_id: str, action: str, updated_by: str | None
    ) -> None:
        self.calls.append((tenant_id, action, updated_by))
        if self._raise_on_tenant is not None and tenant_id == self._raise_on_tenant:
            raise RuntimeError("simulated storage failure")


def _event(payload: dict, *, correlation_id: str | None = "corr-abc") -> Event:
    return Event(
        event_type="memclaw.org.suppression-changed",
        correlation_id=correlation_id,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_happy_path_upserts_each_tenant() -> None:
    adapter = _FakeAdapter()
    await _handle_suppression_changed(
        _event({"tenant_ids": ["t1", "t2"], "action": "suppress"}),
        adapter=adapter,
    )
    assert adapter.calls == [
        ("t1", "suppress", "corr-abc"),
        ("t2", "suppress", "corr-abc"),
    ]


@pytest.mark.asyncio
async def test_restore_action_propagates() -> None:
    adapter = _FakeAdapter()
    await _handle_suppression_changed(
        _event({"tenant_ids": ["t1"], "action": "restore"}),
        adapter=adapter,
    )
    assert adapter.calls == [("t1", "restore", "corr-abc")]


@pytest.mark.asyncio
async def test_malformed_payload_is_dropped() -> None:
    """Missing ``action`` fails Pydantic validation → handler returns
    cleanly. No raise → bus acks → no redelivery loop on a poison message.
    """
    adapter = _FakeAdapter()
    await _handle_suppression_changed(
        _event({"tenant_ids": ["t1"]}),  # missing ``action``
        adapter=adapter,
    )
    assert adapter.calls == []  # no upserts attempted


@pytest.mark.asyncio
async def test_unknown_action_value_is_dropped() -> None:
    """``Literal["suppress", "restore"]`` rejects anything else."""
    adapter = _FakeAdapter()
    await _handle_suppression_changed(
        _event({"tenant_ids": ["t1"], "action": "delete"}),
        adapter=adapter,
    )
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_partial_failure_reraises() -> None:
    """A per-tenant failure must NOT silently advance past — re-raise
    so Pub/Sub redelivers and (eventually) DLQs after max attempts.
    """
    adapter = _FakeAdapter(raise_on_tenant="t2")
    with pytest.raises(RuntimeError, match="simulated storage failure"):
        await _handle_suppression_changed(
            _event({"tenant_ids": ["t1", "t2", "t3"], "action": "suppress"}),
            adapter=adapter,
        )
    # t1 succeeded (idempotent re-upsert is safe on redelivery); t2 raised
    # before t3 ran.
    assert adapter.calls == [
        ("t1", "suppress", "corr-abc"),
        ("t2", "suppress", "corr-abc"),
    ]


@pytest.mark.asyncio
async def test_empty_tenant_ids_is_noop() -> None:
    adapter = _FakeAdapter()
    await _handle_suppression_changed(
        _event({"tenant_ids": [], "action": "suppress"}),
        adapter=adapter,
    )
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_missing_correlation_id_defaults_updated_by() -> None:
    adapter = _FakeAdapter()
    evt = Event(
        event_type="memclaw.org.suppression-changed",
        payload={"tenant_ids": ["t1"], "action": "suppress"},
    )
    # No correlation_id on the envelope.
    assert evt.correlation_id is None
    await _handle_suppression_changed(evt, adapter=adapter)
    assert adapter.calls == [("t1", "suppress", "core-worker")]

"""Consumer-wiring tests for fast-mode governance remediation.

``consumer.handle_memory_enriched`` is where the DEFAULT (fast) write mode
enforces the LLM-signal governance: after the worker PATCHes the LLM's
``contains_pii`` / ``business_relevance`` onto the row, the consumer resolves
the tenant policy and calls ``remediate_after_enrichment``. The remediation
logic itself is unit-tested in ``test_governance_remediation.py``; these pin the
WIRING those tests can't reach — that a policy ``drop`` makes the handler
ack-and-skip contradiction detection (the ``return`` branch in consumer.py), and
that a non-destructive disposition lets detection proceed.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.events.base import Event
from common.events.topics import Topics
from core_api import consumer
from core_api.services.organization_settings import ResolvedConfig

pytestmark = pytest.mark.asyncio


def _event(memory_id, tenant: str = "tenant-gov") -> Event:
    return Event(
        event_type=Topics.Memory.ENRICHED,
        tenant_id=tenant,
        payload={
            "memory_id": str(memory_id),
            "tenant_id": tenant,
            "content": "some enriched memory content",
            "retrieval_hint": "",
        },
    )


@pytest.fixture
def sc(monkeypatch):
    """Mock the storage client bound in BOTH the consumer and the remediation
    module (each captured ``get_storage_client`` at import time)."""
    c = MagicMock()
    c.get_memory = AsyncMock()
    c.soft_delete_memory = AsyncMock()
    c.update_memory = AsyncMock()
    monkeypatch.setattr("core_api.consumer.get_storage_client", lambda: c)
    monkeypatch.setattr(
        "core_api.services.governance_remediation.get_storage_client", lambda: c
    )
    return c


@pytest.fixture
def detect(monkeypatch):
    fn = AsyncMock(return_value=None)
    monkeypatch.setattr("core_api.consumer.detect_contradictions_async", fn)
    return fn


@pytest.fixture(autouse=True)
def _silence_audit(monkeypatch):
    # Isolate from the audit-write plumbing; outcomes are asserted via sc + detect.
    monkeypatch.setattr(
        "core_api.services.governance_remediation.emit_governance_audit", AsyncMock()
    )


def _patch_config(
    monkeypatch, *, pii: dict | None = None, non_business: dict | None = None
) -> None:
    gov: dict = {}
    if pii is not None:
        gov["pii"] = pii
    if non_business is not None:
        gov["non_business"] = non_business
    cfg = ResolvedConfig({"governance": gov})

    async def _resolve(*_a, **_k):
        return cfg

    monkeypatch.setattr("core_api.consumer.resolve_config", _resolve)


def _memory(memory_id, **metadata) -> dict:
    return {
        "id": str(memory_id),
        "tenant_id": "tenant-gov",
        "fleet_id": "fleet-X",
        "content": "some enriched memory content",
        "embedding": [0.1] * 8,  # present → detection isn't deferred
        "metadata": metadata,
    }


async def test_pii_drop_acks_and_skips_detection(sc, detect, monkeypatch):
    mid = uuid.uuid4()
    sc.get_memory.return_value = _memory(mid, contains_pii=True, pii_types=["health"])
    _patch_config(monkeypatch, pii={"enabled": True, "action": "drop"})

    await consumer.handle_memory_enriched(_event(mid))

    sc.soft_delete_memory.assert_awaited_once_with(str(mid))
    detect.assert_not_awaited()  # dropped by policy → contradiction detection skipped


async def test_keep_private_updates_visibility_and_still_runs_detection(
    sc, detect, monkeypatch
):
    mid = uuid.uuid4()
    sc.get_memory.return_value = _memory(mid, business_relevance="personal")
    _patch_config(
        monkeypatch, non_business={"enabled": True, "disposition": "keep_private"}
    )

    await consumer.handle_memory_enriched(_event(mid))

    sc.update_memory.assert_awaited_once_with(str(mid), {"visibility": "scope_agent"})
    detect.assert_awaited_once()  # not dropped → detection proceeds on the kept row


async def test_clean_signal_runs_detection_with_no_governance_action(
    sc, detect, monkeypatch
):
    mid = uuid.uuid4()
    sc.get_memory.return_value = _memory(mid, business_relevance="business")
    _patch_config(
        monkeypatch,
        pii={"enabled": True, "action": "drop"},
        non_business={"enabled": True, "disposition": "drop"},
    )

    await consumer.handle_memory_enriched(_event(mid))

    sc.soft_delete_memory.assert_not_awaited()
    sc.update_memory.assert_not_awaited()
    detect.assert_awaited_once()

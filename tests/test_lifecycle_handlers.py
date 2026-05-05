"""Unit tests for the shared lifecycle archive consumers (CAURA-655).

The handlers live in ``common/events/lifecycle_handlers.py`` so both
core-api (OSS standalone) and core-worker (SaaS) register the same
code. These tests exercise the full success/failure paths against an
in-memory fake adapter — the real adapters are thin httpx wrappers
covered by integration tests elsewhere.
"""

from __future__ import annotations

import pytest

from functools import partial

from common.events.base import Event
from common.events.lifecycle_archive_request import LifecycleArchiveRequest
from common.events.lifecycle_handlers import _run_archive
from common.events.topics import Topics


class _FakeAdapter:
    def __init__(
        self,
        *,
        expired_count: int = 7,
        stale_count: int = 4,
        raise_on_archive: Exception | None = None,
    ):
        self.expired_count = expired_count
        self.stale_count = stale_count
        self.raise_on_archive = raise_on_archive
        self.archive_calls: list[tuple[str, str, str | None]] = []
        self.audit_calls: list[tuple[int, str, dict | None, str | None]] = []

    async def archive_expired(self, *, org_id: str, fleet_id: str | None) -> int:
        self.archive_calls.append(("expired", org_id, fleet_id))
        if self.raise_on_archive is not None:
            raise self.raise_on_archive
        return self.expired_count

    async def archive_stale(self, *, org_id: str, fleet_id: str | None) -> int:
        self.archive_calls.append(("stale", org_id, fleet_id))
        if self.raise_on_archive is not None:
            raise self.raise_on_archive
        return self.stale_count

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        self.audit_calls.append((audit_id, status, stats, error_message))


def _event(
    topic: str,
    *,
    audit_id: int = 42,
    org_id: str = "tenant-x",
    fleet_id: str | None = None,
) -> Event:
    payload = LifecycleArchiveRequest(
        audit_id=audit_id,
        org_id=org_id,
        triggered_by="test",
        fleet_id=fleet_id,
    ).model_dump(mode="json")
    return Event(event_type=topic, payload=payload)


def _bind(adapter: _FakeAdapter, *, action: str):
    """Mirror what register_consumers() does at app startup — bind the
    adapter and the per-action archive callable into the dispatch."""
    archive_fn = (
        adapter.archive_expired
        if action == "archive-expired"
        else adapter.archive_stale
    )
    return partial(_run_archive, adapter=adapter, archive_fn=archive_fn, action=action)


@pytest.mark.asyncio
async def test_archive_expired_success_marks_audit_progress_then_success():
    adapter = _FakeAdapter(expired_count=11)
    handler = _bind(adapter, action="archive-expired")
    await handler(_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))
    # Order matters: in_progress must land BEFORE the storage primitive
    # so observers can distinguish a stuck-in-progress run from a
    # never-started one.
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "success"]
    final = adapter.audit_calls[-1]
    assert final[0] == 42
    assert final[2] == {"archived": 11}
    assert final[3] is None
    assert adapter.archive_calls == [("expired", "tenant-x", None)]


@pytest.mark.asyncio
async def test_archive_stale_dispatches_to_stale_primitive():
    adapter = _FakeAdapter(stale_count=3)
    handler = _bind(adapter, action="archive-stale")
    await handler(_event(Topics.Lifecycle.ARCHIVE_STALE_REQUESTED, fleet_id="fleet-1"))
    assert adapter.archive_calls == [("stale", "tenant-x", "fleet-1")]
    assert adapter.audit_calls[-1] == (42, "success", {"archived": 3}, None)


@pytest.mark.asyncio
async def test_archive_failure_marks_audit_failure_and_reraises():
    err = RuntimeError("storage down")
    adapter = _FakeAdapter(raise_on_archive=err)
    handler = _bind(adapter, action="archive-expired")
    with pytest.raises(RuntimeError, match="storage down"):
        await handler(_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "failure"]
    final = adapter.audit_calls[-1]
    assert final[3] == "storage down"
    assert final[2] is None  # no stats on failure path


@pytest.mark.asyncio
async def test_failure_audit_update_error_does_not_swallow_original():
    """If the audit-row failure update itself raises, the original
    archive exception must still propagate. Otherwise the row would
    sit in ``in_progress`` indefinitely AND Pub/Sub would see the wrong
    exception (audit-update flake instead of the real archive failure).
    """

    class _FlakyAuditAdapter(_FakeAdapter):
        async def update_lifecycle_audit_row(
            self,
            audit_id: int,
            *,
            status: str,
            stats: dict | None = None,
            error_message: str | None = None,
        ) -> None:
            self.audit_calls.append((audit_id, status, stats, error_message))
            if status == "failure":
                raise RuntimeError("audit endpoint down")

    adapter = _FlakyAuditAdapter(raise_on_archive=RuntimeError("storage down"))
    handler = _bind(adapter, action="archive-expired")
    with pytest.raises(RuntimeError, match="storage down"):
        await handler(_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "failure"]


@pytest.mark.asyncio
async def test_malformed_payload_is_acked_dropped():
    adapter = _FakeAdapter()
    handler = _bind(adapter, action="archive-expired")
    bad_event = Event(
        event_type=Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED,
        payload={"audit_id": "not-an-int"},
    )
    await handler(bad_event)
    # No archive call, no audit-row PATCH — the message gets dropped
    # cleanly so a poison message can't loop the subscription.
    assert adapter.archive_calls == []
    assert adapter.audit_calls == []

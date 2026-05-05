"""Unit tests for the shared lifecycle action consumers (CAURA-655 + CAURA-656).

The handlers live in ``common/events/lifecycle_handlers.py`` so both
core-api (OSS standalone) and core-worker (SaaS) register the same
code. These tests exercise the full success/failure paths against an
in-memory fake adapter — the real adapters are thin httpx wrappers
covered by integration tests elsewhere.
"""

from __future__ import annotations

from functools import partial

import pytest

from common.events.base import Event
from common.events.lifecycle_archive_request import LifecycleArchiveRequest
from common.events.lifecycle_handlers import _run_action
from common.events.lifecycle_purge_request import LifecyclePurgeRequest
from common.events.topics import Topics


class _FakeAdapter:
    def __init__(
        self,
        *,
        expired_count: int = 7,
        stale_count: int = 4,
        purged_count: int = 5,
        raise_on_op: Exception | None = None,
    ):
        self.expired_count = expired_count
        self.stale_count = stale_count
        self.purged_count = purged_count
        self.raise_on_op = raise_on_op
        self.archive_calls: list[tuple[str, str, str | None, int | None]] = []
        self.audit_calls: list[tuple[int, str, dict | None, str | None]] = []

    async def archive_expired(self, *, org_id: str, fleet_id: str | None) -> int:
        self.archive_calls.append(("expired", org_id, fleet_id, None))
        if self.raise_on_op is not None:
            raise self.raise_on_op
        return self.expired_count

    async def archive_stale(self, *, org_id: str, fleet_id: str | None) -> int:
        self.archive_calls.append(("stale", org_id, fleet_id, None))
        if self.raise_on_op is not None:
            raise self.raise_on_op
        return self.stale_count

    async def purge_soft_deleted(
        self, *, org_id: str, fleet_id: str | None, retention_days: int
    ) -> int:
        self.archive_calls.append(("purge", org_id, fleet_id, retention_days))
        if self.raise_on_op is not None:
            raise self.raise_on_op
        return self.purged_count

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        self.audit_calls.append((audit_id, status, stats, error_message))


def _archive_event(
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


def _purge_event(
    *,
    audit_id: int = 99,
    org_id: str = "tenant-x",
    fleet_id: str | None = None,
    retention_days: int = 14,
) -> Event:
    payload = LifecyclePurgeRequest(
        audit_id=audit_id,
        org_id=org_id,
        triggered_by="test",
        fleet_id=fleet_id,
        retention_days=retention_days,
    ).model_dump(mode="json")
    return Event(
        event_type=Topics.Lifecycle.PURGE_SOFT_DELETED_REQUESTED,
        payload=payload,
    )


def _bind(adapter: _FakeAdapter, *, action: str):
    """Mirror what ``register_consumers`` does at app startup — bind
    the adapter and the per-action archive callable into the dispatch
    via :func:`functools.partial`. Single helper so adding a new
    action only requires extending the lookup table.
    """
    if action == "archive-expired":

        async def _op(req: LifecycleArchiveRequest) -> int:
            return await adapter.archive_expired(
                org_id=req.org_id, fleet_id=req.fleet_id
            )

        payload_cls: type = LifecycleArchiveRequest
        stats_key = "archived"
    elif action == "archive-stale":

        async def _op(req: LifecycleArchiveRequest) -> int:
            return await adapter.archive_stale(org_id=req.org_id, fleet_id=req.fleet_id)

        payload_cls = LifecycleArchiveRequest
        stats_key = "archived"
    elif action == "purge-soft-deleted":

        async def _op(req: LifecyclePurgeRequest) -> int:
            return await adapter.purge_soft_deleted(
                org_id=req.org_id,
                fleet_id=req.fleet_id,
                retention_days=req.retention_days,
            )

        payload_cls = LifecyclePurgeRequest
        stats_key = "deleted"
    else:
        raise ValueError(f"unknown action {action!r}")

    return partial(
        _run_action,
        adapter=adapter,
        payload_cls=payload_cls,
        run_op=_op,
        stats_key=stats_key,
        action=action,
    )


@pytest.mark.asyncio
async def test_archive_expired_success_marks_audit_progress_then_success():
    adapter = _FakeAdapter(expired_count=11)
    handler = _bind(adapter, action="archive-expired")
    await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))
    # Order matters: in_progress must land BEFORE the storage primitive
    # so observers can distinguish a stuck-in-progress run from a
    # never-started one.
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "success"]
    final = adapter.audit_calls[-1]
    assert final[0] == 42
    assert final[2] == {"archived": 11}
    assert final[3] is None
    assert adapter.archive_calls == [("expired", "tenant-x", None, None)]


@pytest.mark.asyncio
async def test_archive_stale_dispatches_to_stale_primitive():
    adapter = _FakeAdapter(stale_count=3)
    handler = _bind(adapter, action="archive-stale")
    await handler(
        _archive_event(Topics.Lifecycle.ARCHIVE_STALE_REQUESTED, fleet_id="fleet-1")
    )
    assert adapter.archive_calls == [("stale", "tenant-x", "fleet-1", None)]
    assert adapter.audit_calls[-1] == (42, "success", {"archived": 3}, None)


@pytest.mark.asyncio
async def test_purge_soft_deleted_forwards_retention_days_and_uses_deleted_stats_key():
    adapter = _FakeAdapter(purged_count=8)
    handler = _bind(adapter, action="purge-soft-deleted")
    await handler(_purge_event(retention_days=7, fleet_id="fleet-2"))
    # The op was called with retention_days from the payload.
    assert adapter.archive_calls == [("purge", "tenant-x", "fleet-2", 7)]
    # Stats key is 'deleted', not 'archived' — the only per-action
    # divergence in the success branch.
    assert adapter.audit_calls[-1] == (99, "success", {"deleted": 8}, None)


@pytest.mark.asyncio
async def test_archive_failure_marks_audit_failure_and_reraises():
    err = RuntimeError("storage down")
    adapter = _FakeAdapter(raise_on_op=err)
    handler = _bind(adapter, action="archive-expired")
    with pytest.raises(RuntimeError, match="storage down"):
        await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "failure"]
    final = adapter.audit_calls[-1]
    assert final[3] == "storage down"
    assert final[2] is None  # no stats on failure path


@pytest.mark.asyncio
async def test_failure_audit_update_error_does_not_swallow_original():
    """If the audit-row failure update itself raises, the original
    op exception must still propagate. Otherwise the row would sit
    in ``in_progress`` indefinitely AND Pub/Sub would see the wrong
    exception (audit-update flake instead of the real op failure).
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

    adapter = _FlakyAuditAdapter(raise_on_op=RuntimeError("storage down"))
    handler = _bind(adapter, action="archive-expired")
    with pytest.raises(RuntimeError, match="storage down"):
        await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "failure"]


@pytest.mark.asyncio
async def test_malformed_archive_payload_is_acked_dropped():
    adapter = _FakeAdapter()
    handler = _bind(adapter, action="archive-expired")
    bad_event = Event(
        event_type=Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED,
        payload={"audit_id": "not-an-int"},
    )
    await handler(bad_event)
    assert adapter.archive_calls == []
    assert adapter.audit_calls == []


@pytest.mark.asyncio
async def test_malformed_purge_payload_is_acked_dropped():
    """Purge payload requires retention_days in [1, 30]. A missing
    field or out-of-range value must drop the message rather than
    leak a 500 / nack-loop.
    """
    adapter = _FakeAdapter()
    handler = _bind(adapter, action="purge-soft-deleted")
    # Missing retention_days entirely.
    bad_event = Event(
        event_type=Topics.Lifecycle.PURGE_SOFT_DELETED_REQUESTED,
        payload={
            "audit_id": 1,
            "org_id": "tenant-x",
            "triggered_by": "test",
        },
    )
    await handler(bad_event)
    # retention_days out of range — bumps against the Field(le=30)
    # constraint in LifecyclePurgeRequest.
    out_of_range = Event(
        event_type=Topics.Lifecycle.PURGE_SOFT_DELETED_REQUESTED,
        payload={
            "audit_id": 2,
            "org_id": "tenant-x",
            "triggered_by": "test",
            "retention_days": 99,
        },
    )
    await handler(out_of_range)
    assert adapter.archive_calls == []
    assert adapter.audit_calls == []

"""Unit tests for the shared lifecycle action consumers (CAURA-655 +
CAURA-656 + CAURA-657).

The handlers live in ``common/events/lifecycle_handlers.py`` so both
core-api (always, for pipeline ops) and core-worker (SaaS, for
archive ops) register the same code. These tests exercise the full
success / failure / dedup paths against an in-memory fake adapter —
the real adapters are thin wrappers covered by integration tests.
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
        crystallized_count: int = 1,
        entity_linked_count: int = 12,
        insights_count: int = 3,
        raise_on_op: Exception | None = None,
        has_recent_success: bool = False,
        raise_on_dedup_check: Exception | None = None,
    ):
        self.expired_count = expired_count
        self.stale_count = stale_count
        self.purged_count = purged_count
        self.crystallized_count = crystallized_count
        self.entity_linked_count = entity_linked_count
        self.insights_count = insights_count
        self.raise_on_op = raise_on_op
        self.has_recent_success_value = has_recent_success
        self.raise_on_dedup_check = raise_on_dedup_check
        self.archive_calls: list[tuple[str, str, str | None, int | None]] = []
        self.audit_calls: list[tuple[int, str, dict | None, str | None]] = []
        self.dedup_calls: list[tuple[str, str, int]] = []

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

    async def crystallize(self, *, org_id: str, fleet_id: str | None) -> int:
        self.archive_calls.append(("crystallize", org_id, fleet_id, None))
        if self.raise_on_op is not None:
            raise self.raise_on_op
        return self.crystallized_count

    async def entity_link(self, *, org_id: str, fleet_id: str | None) -> int:
        self.archive_calls.append(("entity-link", org_id, fleet_id, None))
        if self.raise_on_op is not None:
            raise self.raise_on_op
        return self.entity_linked_count

    async def insights(self, *, org_id: str, fleet_id: str | None) -> int:
        self.archive_calls.append(("insights", org_id, fleet_id, None))
        if self.raise_on_op is not None:
            raise self.raise_on_op
        return self.insights_count

    async def has_recent_lifecycle_success(
        self, *, org_id: str, action: str, since_hours: int
    ) -> bool:
        self.dedup_calls.append((org_id, action, since_hours))
        if self.raise_on_dedup_check is not None:
            raise self.raise_on_dedup_check
        return self.has_recent_success_value

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


def _bind(adapter: _FakeAdapter, *, action: str, dedup_window_hours: int | None = None):
    """Mirror what ``register_*_consumers`` do at app startup — bind
    the adapter and the per-action callable into the dispatch via
    :func:`functools.partial`. Pipeline ops set ``dedup_window_hours``
    so the handler exercises the gate.
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
    elif action == "crystallize":

        async def _op(req: LifecycleArchiveRequest) -> int:
            return await adapter.crystallize(org_id=req.org_id, fleet_id=req.fleet_id)

        payload_cls = LifecycleArchiveRequest
        stats_key = "links_or_clusters"
    elif action == "entity-link":

        async def _op(req: LifecycleArchiveRequest) -> int:
            return await adapter.entity_link(org_id=req.org_id, fleet_id=req.fleet_id)

        payload_cls = LifecycleArchiveRequest
        stats_key = "links_created"
    elif action == "insights":

        async def _op(req: LifecycleArchiveRequest) -> int:
            return await adapter.insights(org_id=req.org_id, fleet_id=req.fleet_id)

        payload_cls = LifecycleArchiveRequest
        stats_key = "insights_created"
    else:
        raise ValueError(f"unknown action {action!r}")

    return partial(
        _run_action,
        adapter=adapter,
        payload_cls=payload_cls,
        run_op=_op,
        stats_key=stats_key,
        action=action,
        dedup_window_hours=dedup_window_hours,
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


# ── CAURA-657: pipeline ops + dedup gate ─────────────────────────────


@pytest.mark.asyncio
async def test_crystallize_runs_when_no_recent_success():
    adapter = _FakeAdapter(crystallized_count=1, has_recent_success=False)
    handler = _bind(adapter, action="crystallize", dedup_window_hours=23)
    await handler(_archive_event(Topics.Lifecycle.CRYSTALLIZE_REQUESTED))
    assert adapter.dedup_calls == [("tenant-x", "crystallize", 23)]
    assert adapter.archive_calls == [("crystallize", "tenant-x", None, None)]
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "success"]
    assert adapter.audit_calls[-1][2] == {"links_or_clusters": 1}


@pytest.mark.asyncio
async def test_entity_link_runs_when_no_recent_success():
    adapter = _FakeAdapter(entity_linked_count=42)
    handler = _bind(adapter, action="entity-link", dedup_window_hours=23)
    await handler(_archive_event(Topics.Lifecycle.ENTITY_LINK_REQUESTED))
    assert adapter.archive_calls == [("entity-link", "tenant-x", None, None)]
    assert adapter.audit_calls[-1][2] == {"links_created": 42}


@pytest.mark.asyncio
async def test_insights_runs_when_no_recent_success():
    adapter = _FakeAdapter(insights_count=7, has_recent_success=False)
    handler = _bind(adapter, action="insights", dedup_window_hours=23)
    await handler(_archive_event(Topics.Lifecycle.INSIGHTS_REQUESTED))
    # Dedup gate consulted with the same 23h window the pipeline ops use.
    assert adapter.dedup_calls == [("tenant-x", "insights", 23)]
    # Primitive invoked with the published payload.
    assert adapter.archive_calls == [("insights", "tenant-x", None, None)]
    # Audit transitions: in_progress → success carrying the new stats key.
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "success"]
    assert adapter.audit_calls[-1][2] == {"insights_created": 7}


@pytest.mark.asyncio
async def test_insights_dedup_gate_skips_when_recent_success_exists():
    """Insights inherits the 23h pipeline dedup gate: a successful
    run in the window short-circuits to a no-op success record with
    ``stats={skipped: True}`` and the adapter primitive is never invoked.
    """
    adapter = _FakeAdapter(has_recent_success=True)
    handler = _bind(adapter, action="insights", dedup_window_hours=23)
    await handler(_archive_event(Topics.Lifecycle.INSIGHTS_REQUESTED))
    assert adapter.archive_calls == []
    assert len(adapter.audit_calls) == 1
    _, status, stats, error = adapter.audit_calls[0]
    assert status == "success"
    assert stats == {"skipped": True, "reason": "recent_success"}
    assert error is None


@pytest.mark.asyncio
async def test_pipeline_dedup_gate_skips_when_recent_success_exists():
    """Dedup gate: when has_recent_lifecycle_success returns True, the
    handler must NOT call the primitive and must mark the audit row
    success with stats={skipped: True}.
    """
    adapter = _FakeAdapter(has_recent_success=True)
    handler = _bind(adapter, action="crystallize", dedup_window_hours=23)
    await handler(_archive_event(Topics.Lifecycle.CRYSTALLIZE_REQUESTED))
    # Primitive never invoked.
    assert adapter.archive_calls == []
    # Audit row marked success with skipped flag — no in_progress
    # flicker (gate runs before in_progress).
    assert len(adapter.audit_calls) == 1
    audit_id, status, stats, error = adapter.audit_calls[0]
    assert status == "success"
    assert stats == {"skipped": True, "reason": "recent_success"}
    assert error is None


@pytest.mark.asyncio
async def test_pipeline_dedup_check_failure_falls_through_to_run_op():
    """If the dedup gate itself fails (storage flake), proceed with
    the op — better to run twice than skip a legitimate request
    because the gate endpoint flaked.
    """
    adapter = _FakeAdapter(
        crystallized_count=3,
        raise_on_dedup_check=RuntimeError("storage 503"),
    )
    handler = _bind(adapter, action="crystallize", dedup_window_hours=23)
    await handler(_archive_event(Topics.Lifecycle.CRYSTALLIZE_REQUESTED))
    # Primitive ran despite the gate failure.
    assert adapter.archive_calls == [("crystallize", "tenant-x", None, None)]
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "success"]


@pytest.mark.asyncio
async def test_archive_op_does_not_invoke_dedup_gate():
    """Archive ops are naturally idempotent (SQL primitive returns 0
    if there's nothing to do); skipping the dedup gate avoids a
    pointless storage round-trip on every redelivery.
    """
    adapter = _FakeAdapter(expired_count=11)
    handler = _bind(adapter, action="archive-expired")  # no dedup_window
    await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))
    assert adapter.dedup_calls == []

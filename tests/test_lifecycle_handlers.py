"""Unit tests for the shared lifecycle action consumers (CAURA-655 +
CAURA-656 + CAURA-657).

The handlers live in ``common/events/lifecycle_handlers.py`` so both
core-api (always, for pipeline ops) and core-worker (SaaS, for
archive ops) register the same code. These tests exercise the full
success / failure / dedup paths against an in-memory fake adapter —
the real adapters are thin wrappers covered by integration tests.
"""

from __future__ import annotations

import logging
from functools import partial

import pytest

from common.events.base import Event, PermanentOpError
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
        self.audit_org_ids: list[str] = []
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
        org_id: str,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
        claim_token: str | None = None,
    ) -> None:
        self.audit_org_ids.append(org_id)
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
    assert adapter.audit_org_ids == ["tenant-x", "tenant-x"]
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
            org_id: str,
            status: str,
            stats: dict | None = None,
            error_message: str | None = None,
            claim_token: str | None = None,
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


@pytest.mark.asyncio
async def test_terminal_op_error_marks_failure_but_does_not_reraise():
    """A ``PermanentOpError`` means "failed, and a retry cannot help".

    The runner previously had one failure path, which always re-raised so the bus
    nacked. That conflates "did it succeed?" with "should it be retried?" — for a
    deterministic bug (a wrongly-shaped injectable hitting every cluster, every
    tick) the honest answers differ, and each redelivery of the Forge tick pays for
    an LLM call per cluster before failing identically.

    So: same durable ``failure`` row as any other exception, but ACK.
    """

    err = PermanentOpError("wrote nothing; 2 programming errors — code/wiring bug")
    adapter = _FakeAdapter(raise_on_op=err)
    handler = _bind(adapter, action="archive-expired")

    # No pytest.raises: propagating is exactly what must NOT happen here.
    await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))

    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "failure"], statuses
    final = adapter.audit_calls[-1]
    assert final[3] == "wrote nothing; 2 programming errors — code/wiring bug"
    # ``stats`` is what makes the two failure classes distinguishable in the
    # DURABLE record. Without it, "a human must act" and "four more attempts are
    # coming" are byte-identical rows and nobody can query or alert on the
    # difference — which would undercut the point of fixing the verdict at all.
    assert final[2] == {"terminal": True}


@pytest.mark.asyncio
async def test_terminal_failure_is_not_recorded_as_a_recent_success():
    """The reason the verdict matters: ``has_recent_lifecycle_success`` gates
    re-runs on a SUCCESS row. A terminal failure must leave that gate open so the
    operator can re-run to reproduce — the bug this whole change exists to stop is
    a false success blocking its own diagnosis."""

    adapter = _FakeAdapter(raise_on_op=PermanentOpError("terminal"))
    handler = _bind(adapter, action="archive-expired")

    await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))

    assert "success" not in [c[1] for c in adapter.audit_calls]


@pytest.mark.asyncio
async def test_permanent_error_still_reraises_when_the_failure_row_could_not_be_written():
    """Acking a permanent failure is only defensible because the ``failure`` row is
    the durable record. When that write fails there IS no record, so this path must
    fall through to the raise and let redelivery try again to produce one.

    Otherwise the ack makes a row stuck in ``in_progress`` permanent — exactly the
    state indistinguishable from a crashed worker that the generic path's guard
    exists to avoid. One wasted retry is the cheaper trade.
    """

    class _FlakyAuditAdapter(_FakeAdapter):
        async def update_lifecycle_audit_row(
            self,
            audit_id: int,
            *,
            org_id: str,
            status: str,
            stats: dict | None = None,
            error_message: str | None = None,
            claim_token: str | None = None,
        ) -> None:
            self.audit_calls.append((audit_id, status, stats, error_message))
            if status == "failure":
                raise RuntimeError("audit endpoint down")

    adapter = _FlakyAuditAdapter(raise_on_op=PermanentOpError("wiring bug"))
    handler = _bind(adapter, action="archive-expired")

    # The ORIGINAL error propagates, not the audit flake — the bus must see why
    # the op failed, and the audit error would be a misleading substitute.
    with pytest.raises(PermanentOpError, match="wiring bug"):
        await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))


@pytest.mark.asyncio
async def test_skip_write_failure_nacks_instead_of_stranding_the_row():
    """A flaked skip-write must NACK, not ack.

    The skip row is the only durable record that this delivery was
    consumed and consciously did nothing. Acking when that write failed
    strands the row at ``pending`` with nothing to retry it and no
    reconciler to sweep it, and the deploy-gate smoke then reads it as an
    unfinished op for the remaining 30h of its window — which is exactly
    what prod audit 73668 did on 2026-09-15.

    Raising hands the delivery back to the bus, whose redelivery is
    bounded by max-delivery-attempts → DLQ: a visible, alertable failure
    instead of an invisible permanent one. Safe on this path specifically
    because a redelivered skip re-checks the gate and skips again.
    """

    class _FlakySkipAdapter(_FakeAdapter):
        async def update_lifecycle_audit_row(
            self,
            audit_id: int,
            *,
            org_id: str,
            status: str,
            stats: dict | None = None,
            error_message: str | None = None,
            claim_token: str | None = None,
        ) -> None:
            self.audit_calls.append((audit_id, status, stats, error_message))
            if stats is not None and stats.get("skipped"):
                raise RuntimeError("audit endpoint down")

    adapter = _FlakySkipAdapter(has_recent_success=True)
    handler = _bind(adapter, action="crystallize", dedup_window_hours=23)

    with pytest.raises(RuntimeError, match="audit endpoint down"):
        await handler(_archive_event(Topics.Lifecycle.CRYSTALLIZE_REQUESTED))

    # The primitive still must not run — this is the skip path, and the
    # nack is about recording the skip, not about redoing the work.
    assert adapter.archive_calls == []
    # Exactly one write attempt, and it was the skip record.
    assert len(adapter.audit_calls) == 1
    assert adapter.audit_calls[0][1] == "success"
    assert adapter.audit_calls[0][2] == {"skipped": True, "reason": "recent_success"}


@pytest.mark.asyncio
async def test_losing_the_claim_skips_the_primitive_and_nacks():
    """A consumer that loses the pending -> in_progress race must not run.

    Two deliveries of one audit_id are reachable: the reconcile sweep
    republishes a message whose original was merely queued (these
    subscriptions retain for seven days, so no age threshold separates
    "lost" from "slow"), or Pub/Sub redelivers while the first attempt is
    still running. The dedup gate does not help — it only matches work that
    already SUCCEEDED, and neither racer has finished. Without the claim,
    both run the primitive: for crystallize or insights that is duplicate
    LLM spend and duplicate records.

    It must raise rather than return. Acking would drop this delivery for
    good, and if the claim holder then died the row would sit at
    in_progress, where the reconcile sweep deliberately does not look.
    """

    class _ClaimedAdapter(_FakeAdapter):
        async def update_lifecycle_audit_row(
            self,
            audit_id: int,
            *,
            org_id: str,
            status: str,
            stats: dict | None = None,
            error_message: str | None = None,
            claim_token: str | None = None,
        ) -> dict:
            self.audit_calls.append((audit_id, status, stats, error_message))
            return {"ok": True, "claim_conflict": status == "in_progress"}

    adapter = _ClaimedAdapter()
    handler = _bind(adapter, action="archive-expired")
    with pytest.raises(RuntimeError, match="claimed by another consumer"):
        await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))

    assert adapter.archive_calls == [], "primitive ran despite losing the claim"
    # Only the claim attempt itself — no success/failure row written, because
    # this delivery did no work to report.
    assert [c[1] for c in adapter.audit_calls] == ["in_progress"]


@pytest.mark.asyncio
async def test_winning_the_claim_runs_the_primitive_as_before():
    """The guard must only fire on a lost claim, not on every run.

    Pairs with the test above so a change that made the handler skip
    unconditionally — which would pass that one — fails here.
    """

    class _UncontestedAdapter(_FakeAdapter):
        async def update_lifecycle_audit_row(
            self,
            audit_id: int,
            *,
            org_id: str,
            status: str,
            stats: dict | None = None,
            error_message: str | None = None,
            claim_token: str | None = None,
        ) -> dict:
            self.audit_calls.append((audit_id, status, stats, error_message))
            return {"ok": True, "claim_conflict": False}

    adapter = _UncontestedAdapter()
    handler = _bind(adapter, action="archive-expired")
    await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))

    assert len(adapter.archive_calls) == 1
    assert [c[1] for c in adapter.audit_calls] == ["in_progress", "success"]


class _TokenRecordingAdapter(_FakeAdapter):
    """Records the ``claim_token`` presented on every audit write."""

    def __init__(self, *args, claim_lost_on_success: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.tokens: list[tuple[str, str | None]] = []
        self._claim_lost_on_success = claim_lost_on_success

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        org_id: str,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
        claim_token: str | None = None,
    ) -> dict:
        self.audit_org_ids.append(org_id)
        self.audit_calls.append((audit_id, status, stats, error_message))
        self.tokens.append((status, claim_token))
        if status == "success" and self._claim_lost_on_success:
            return {"ok": True, "claim_lost": True}
        return {"ok": True}


@pytest.mark.asyncio
async def test_terminal_write_presents_the_same_token_as_the_claim():
    """A finalize must be attributable to the run that won the claim.

    The storage-side guard can only reject a write from a consumer that
    lost its claim if the write carries the token in the first place. A
    terminal write with no token is admitted unconditionally, so failing
    to thread it here would silently disable the guard rather than break
    anything visibly.
    """
    adapter = _TokenRecordingAdapter(expired_count=4)
    handler = _bind(adapter, action="archive-expired")
    await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))

    by_status = dict(adapter.tokens)
    assert set(by_status) == {"in_progress", "success"}
    assert by_status["in_progress"] is not None, "claim must mint a token"
    assert by_status["success"] == by_status["in_progress"], (
        "the terminal write must present the claim's own token, not a new "
        f"one: {adapter.tokens}"
    )


@pytest.mark.asyncio
async def test_claim_lost_on_finalize_is_logged_as_an_error(caplog):
    """Losing the claim mid-run means the primitive ran twice.

    Nothing here can undo that, and retrying would make it three times, so
    the handler must not raise. What it must not do is stay silent: this is
    the only place the duplicate becomes visible.
    """
    adapter = _TokenRecordingAdapter(expired_count=4, claim_lost_on_success=True)
    handler = _bind(adapter, action="archive-expired")

    with caplog.at_level(logging.ERROR):
        await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a lost claim on a terminal write must be logged at error"
    assert any("without its claim" in r.getMessage() for r in errors), (
        f"expected the duplicate-run message, got {[r.getMessage() for r in errors]}"
    )


@pytest.mark.asyncio
async def test_redelivery_of_a_succeeded_row_does_not_rerun_the_primitive():
    """A ``noop`` claim means the row already reached ``success``.

    Re-running is the duplicate this path exists to avoid, and it is also what
    would make the terminal write look like a lost claim: the winner's token is
    on the row and this delivery's is not, so a routine redelivery would be
    reported as a duplicate run. Acking here keeps both from happening.
    """

    class _AlreadySucceededAdapter(_TokenRecordingAdapter):
        async def update_lifecycle_audit_row(
            self,
            audit_id: int,
            *,
            org_id: str,
            status: str,
            stats: dict | None = None,
            error_message: str | None = None,
            claim_token: str | None = None,
        ) -> dict:
            await super().update_lifecycle_audit_row(
                audit_id,
                org_id=org_id,
                status=status,
                stats=stats,
                error_message=error_message,
                claim_token=claim_token,
            )
            return {"ok": True, "noop": status == "in_progress"}

    adapter = _AlreadySucceededAdapter(expired_count=7)
    handler = _bind(adapter, action="archive-expired")
    await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))

    assert adapter.archive_calls == [], (
        "the primitive re-ran on a row that had already succeeded: "
        f"{adapter.archive_calls}"
    )
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress"], (
        f"a redelivery must not write a terminal status, got {statuses}"
    )


@pytest.mark.asyncio
async def test_claim_lost_on_the_failure_path_is_also_logged(caplog):
    """The preempted run that FAILS is the more alarming half.

    Reporting a lost claim only on the success path would make a duplicate
    visible exactly when it cost least. Here two consumers ran, the result
    standing on the row belongs to the other one, and this run is about to
    raise — none of which is inferable from the exception alone.
    """

    class _LostOnFailureAdapter(_TokenRecordingAdapter):
        async def update_lifecycle_audit_row(
            self,
            audit_id: int,
            *,
            org_id: str,
            status: str,
            stats: dict | None = None,
            error_message: str | None = None,
            claim_token: str | None = None,
        ) -> dict:
            await super().update_lifecycle_audit_row(
                audit_id,
                org_id=org_id,
                status=status,
                stats=stats,
                error_message=error_message,
                claim_token=claim_token,
            )
            return {"ok": True, "claim_lost": status == "failure"}

    adapter = _LostOnFailureAdapter(raise_on_op=RuntimeError("storage down"))
    handler = _bind(adapter, action="archive-expired")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="storage down"):
            await handler(_archive_event(Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED))

    assert any(
        "failed without its claim" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.ERROR
    ), (
        "a lost claim on the failure path was not reported: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    # The original op error must still propagate — the log is additive.
    statuses = [c[1] for c in adapter.audit_calls]
    assert statuses == ["in_progress", "failure"]

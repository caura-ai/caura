"""The reconcile sweep must complete stranded rows, not paper over them.

A fanout writes its audit row before publishing the per-org message. When
the request is cancelled part-way through its ``gather`` -- as production's
was on 2026-09-16, after core-api blew a 45s budget -- the rows already
written never publish. Nothing retried them: no redelivery, because no
message ever existed; no reconciler; no sweep. Two ``archive-expired`` rows
sat at ``pending`` for hours and failed the post-deploy smoke, which asserts
every audit row in its 30h window reached ``success``.

These tests pin the three properties that make the sweep a repair rather
than a cosmetic: it finishes the ORIGINAL row, it cannot become a second
unbounded fanout, and it will not republish a job whose scope it cannot
reconstruct.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _row(*, age_minutes: int = 45, **over: object) -> dict:
    """One stranded row, ``age_minutes`` old.

    The age is computed from now rather than hardcoded because the route
    reads it: past the strand threshold (30m) the row is a candidate, and
    past the ineffective threshold (150m) it also counts as a row earlier
    sweeps failed to repair. A fixed literal would silently drift from
    "recently stranded" to "long-unrepaired" as the clock moved.
    """
    row = {
        "audit_id": 73668,
        "org_id": "debug-graph-26f2ad",
        "action": "crystallize",
        "triggered_by": "core-operations",
        "started_at": (datetime.now(UTC) - timedelta(minutes=age_minutes)).isoformat(),
    }
    row.update(over)
    return row


def _storage(rows: list[dict]) -> MagicMock:
    storage = MagicMock()
    storage.list_stranded_lifecycle_audits = AsyncMock(return_value=rows)
    storage.update_lifecycle_audit_row = AsyncMock(return_value=None)
    return storage


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sweep_republishes_under_the_original_audit_id() -> None:
    """The republished message must carry the STRANDED row's id.

    Publishing a fresh row instead would make the totals look healthier
    while leaving the stuck row exactly as stuck -- and the stuck row is
    the one the smoke gate reads. Asserts on the id handed to the
    publisher, so a sweep that allocates a new row fails here rather than
    passing on a correct-looking count.
    """
    from core_api.routes import lifecycle

    published: list[dict] = []

    async def _pub(**kwargs: object) -> None:
        published.append(dict(kwargs))

    storage = _storage([_row()])
    with (
        patch.object(lifecycle, "get_storage_client", lambda: storage),
        patch.dict(lifecycle._ACTION_PUBLISHERS, {"crystallize": _pub}),
        patch.object(
            lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
        ),
    ):
        result = await lifecycle.reconcile_stranded_lifecycle_actions(auth=MagicMock())

    assert len(published) == 1
    assert published[0]["audit_id"] == 73668
    assert published[0]["org_id"] == "debug-graph-26f2ad"
    assert result == {
        "stranded": 1,
        "republish_attempted": 1,
        "failed": 0,
        "unknown_action": 0,
        "ineffective": 0,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sweep_only_asks_for_fanout_rows() -> None:
    """Manual rows are out of scope and must not even be fetched.

    A manual trigger may have carried a ``fleet_id`` or a one-off
    ``retention_days``; neither is stored on the audit row, so
    republishing one would run a different job than the row records.
    The guard belongs in the query, not in a later filter, so the
    unreconstructable rows are never candidates at all.
    """
    from core_api.routes import lifecycle

    storage = _storage([])
    with patch.object(lifecycle, "get_storage_client", lambda: storage):
        await lifecycle.reconcile_stranded_lifecycle_actions(auth=MagicMock())

    kwargs = storage.list_stranded_lifecycle_audits.await_args.kwargs
    assert kwargs["triggered_by"] == "core-operations"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sweep_shares_the_fanout_budget_rather_than_opening_its_own() -> None:
    """The repair must not reproduce the failure it repairs.

    An unbounded fanout against core-storage-api is what stranded these
    rows. A sweep with its own budget would add a second burst on the
    same writer. Asserts on PEAK simultaneous publishes, so it fails for
    a private budget however that budget is spelled.
    """
    from core_api.routes import lifecycle

    in_flight = 0
    peak = 0

    async def _pub(**_kwargs: object) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            in_flight -= 1

    rows = [_row(audit_id=i, org_id=f"org-{i}") for i in range(10)]
    cache = getattr(lifecycle, "_FANOUT_SEMAPHORES", None)
    if cache is not None:
        cache.clear()
    try:
        with (
            patch.object(lifecycle, "_FANOUT_CONCURRENCY", 2),
            patch.object(lifecycle, "get_storage_client", lambda: _storage(rows)),
            patch.dict(lifecycle._ACTION_PUBLISHERS, {"crystallize": _pub}),
            patch.object(
                lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
            ),
        ):
            await lifecycle.reconcile_stranded_lifecycle_actions(auth=MagicMock())
    finally:
        if cache is not None:
            cache.clear()

    assert peak <= 2, f"sweep ran {peak} concurrent publishes against a budget of 2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_failed_republish_does_not_abort_the_rest() -> None:
    """A single bad org must not strand the other rows for another hour.

    The failing row is deliberately left ``pending`` rather than
    finalized: the next sweep retries it, whereas marking it failed would
    record the sweep's own problem as the lifecycle job's outcome.
    """
    from core_api.routes import lifecycle

    published: list[int] = []

    async def _pub(**kwargs: object) -> None:
        if kwargs["org_id"] == "org-1":
            raise RuntimeError("publish blew up")
        published.append(int(kwargs["audit_id"]))  # type: ignore[call-overload]

    rows = [_row(audit_id=i, org_id=f"org-{i}") for i in range(3)]
    with (
        patch.object(lifecycle, "get_storage_client", lambda: _storage(rows)),
        patch.dict(lifecycle._ACTION_PUBLISHERS, {"crystallize": _pub}),
        patch.object(
            lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
        ),
    ):
        result = await lifecycle.reconcile_stranded_lifecycle_actions(auth=MagicMock())

    assert sorted(published) == [0, 2]
    assert result["republish_attempted"] == 2
    assert result["failed"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retired_action_is_counted_not_raised() -> None:
    """Rows naming an action no longer in the registry can never complete.

    Reporting them keeps that visible; raising would let one retired
    action block every other row in the sweep.
    """
    from core_api.routes import lifecycle

    rows = [_row(action="forge-distill-retired")]
    storage = _storage(rows)
    with (
        patch.object(lifecycle, "get_storage_client", lambda: storage),
        patch.object(
            lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
        ),
    ):
        result = await lifecycle.reconcile_stranded_lifecycle_actions(auth=MagicMock())

    assert result["unknown_action"] == 1
    assert result["republish_attempted"] == 0

    # And it must be FINALIZED, not left pending. An unrunnable row that
    # stays pending sits at the head of every future oldest-first sweep and,
    # once enough accumulate, starves repairable rows out of the LIMIT window.
    storage.update_lifecycle_audit_row.assert_awaited_once()
    call = storage.update_lifecycle_audit_row.await_args
    assert call.args[0] == 73668
    assert call.kwargs["status"] == "failure"
    assert "no longer in the publisher registry" in call.kwargs["error_message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rows_earlier_sweeps_failed_to_repair_are_counted_separately() -> None:
    """A sweep achieving nothing must not report as a healthy one.

    ``PubSubEventBus.publish`` batches and does not await the delivery
    future, so a 403 on an unprovisioned topic never raises here and the
    publish still counts as attempted. Row age is the only evidence the
    endpoint has that earlier attempts did not land: a repaired row leaves
    ``pending`` and stops being selected at all, so one that keeps coming
    back older and older was never delivered.
    """
    from core_api.routes import lifecycle

    async def _pub(**_kwargs: object) -> None:
        return None

    rows = [
        _row(audit_id=1, org_id="org-fresh", age_minutes=45),
        _row(audit_id=2, org_id="org-stuck", age_minutes=600),
    ]
    with (
        patch.object(lifecycle, "get_storage_client", lambda: _storage(rows)),
        patch.dict(lifecycle._ACTION_PUBLISHERS, {"crystallize": _pub}),
        patch.object(
            lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
        ),
    ):
        result = await lifecycle.reconcile_stranded_lifecycle_actions(auth=MagicMock())

    # Both publishes "succeeded" -- neither raised, because none can.
    assert result["republish_attempted"] == 2
    assert result["failed"] == 0
    # Only the long-unrepaired one is reported as the sweep not working.
    assert result["ineffective"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizing_unrepairable_rows_also_shares_the_budget() -> None:
    """The unrepairable path writes to storage too, and must be capped.

    Every row in one sweep can name the same retired action, so an
    unbudgeted finalize would fire up to ``_RECONCILE_MAX_ROWS`` concurrent
    PATCHes at core-storage-api. That is the same unbounded burst the
    publish path is capped to avoid, arriving by the one route that
    skipped the cap.
    """
    from core_api.routes import lifecycle

    in_flight = 0
    peak = 0

    async def _slow_finalize(*_args: object, **_kwargs: object) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            in_flight -= 1

    rows = [
        _row(audit_id=i, org_id=f"org-{i}", action="retired-action") for i in range(10)
    ]
    storage = _storage(rows)
    storage.update_lifecycle_audit_row = AsyncMock(side_effect=_slow_finalize)

    cache = getattr(lifecycle, "_FANOUT_SEMAPHORES", None)
    if cache is not None:
        cache.clear()
    try:
        with (
            patch.object(lifecycle, "_FANOUT_CONCURRENCY", 2),
            patch.object(lifecycle, "get_storage_client", lambda: storage),
            patch.object(
                lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
            ),
        ):
            result = await lifecycle.reconcile_stranded_lifecycle_actions(
                auth=MagicMock()
            )
    finally:
        if cache is not None:
            cache.clear()

    assert result["unknown_action"] == 10
    assert peak <= 2, f"finalize ran {peak} concurrent writes against a budget of 2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rows_finalized_this_sweep_are_not_also_called_ineffective() -> None:
    """A row retired THIS run is terminal, not evidence the sweep is failing.

    ``ineffective`` exists to say "we published and it still did not
    complete". A retired-action row was never published at all and has just
    reached a terminal state, so counting it would fire an error-level alert
    about the sweep on a sweep that did exactly the right thing.
    """
    from core_api.routes import lifecycle

    # Old enough to clear the ineffective threshold on age alone.
    rows = [_row(action="retired-action", age_minutes=600)]
    with (
        patch.object(lifecycle, "get_storage_client", lambda: _storage(rows)),
        patch.object(
            lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
        ),
    ):
        result = await lifecycle.reconcile_stranded_lifecycle_actions(auth=MagicMock())

    assert result["unknown_action"] == 1
    assert result["ineffective"] == 0


@pytest.mark.unit
def test_reconcile_route_resolves_to_the_sweep_not_the_catch_all() -> None:
    """The literal path must win over ``/admin/lifecycle/{action}``.

    Both are three-segment POSTs under the same prefix and Starlette matches
    in registration order, so the literal route works only because it is
    declared first. A reorder would not error -- it would route the sweep into
    ``trigger_lifecycle_action`` and answer "unknown lifecycle action
    'reconcile-stranded'" with a 404, which reads like a deploy problem rather
    than a routing one.

    Resolves a real request against the router rather than comparing list
    indices, so it tests the behaviour that matters rather than a proxy for it.
    """
    from starlette.routing import Match

    from core_api.routes import lifecycle

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/admin/lifecycle/reconcile-stranded",
        "headers": [],
        "root_path": "",
    }
    matched = [
        route
        for route in lifecycle.router.routes
        if route.matches(scope)[0] == Match.FULL
    ]
    assert matched, "no route matched POST /admin/lifecycle/reconcile-stranded"
    assert matched[0].endpoint is lifecycle.reconcile_stranded_lifecycle_actions, (
        f"request resolved to {matched[0].endpoint.__name__}, not the sweep"
    )

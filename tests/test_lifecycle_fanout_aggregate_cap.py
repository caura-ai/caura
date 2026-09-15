"""The nightly lifecycle fanout must not oversubscribe core-storage-api.

Both tests here cover the same 2026-09-15 incident from its two ends: the
burst that dropped 38 orgs, and the log line that reported the sweep as
clean while it happened.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fanout_budget_is_shared_across_concurrent_actions() -> None:
    """Two fanouts running at once share ONE budget, not one each.

    The scheduler fires every lifecycle action on the same cron minute. While
    the semaphore was constructed inside the request handler each of those got
    a fresh budget, so the ceiling the storage writer actually saw was
    ``actions x _FANOUT_CONCURRENCY`` — 8 x 50 in production. That is what
    pushed the writer past its warm capacity and made ``audit_begin`` raise
    for 38 orgs, none of which left an audit row to count.

    Asserts on PEAK simultaneous triggers rather than on the semaphore object,
    so it fails for a per-request budget however the budget is spelled.
    """
    from core_api.routes import lifecycle

    in_flight = 0
    peak = 0

    async def _instrumented(**_kwargs: object) -> int:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
            return 1
        finally:
            in_flight -= 1

    auth = MagicMock()
    orgs = [f"org-{i}" for i in range(10)]

    # Deliberately does NOT patch the semaphore store itself. Referencing a
    # symbol the fix introduces would make this pass-or-AttributeError rather
    # than pass-or-wrong-number, and an AttributeError proves only that a name
    # exists. Clearing it when present (a no-op on the per-request version) is
    # what lets the SAME assertion run against both shapes.
    cache = getattr(lifecycle, "_FANOUT_SEMAPHORES", None)
    if cache is not None:
        cache.clear()
    try:
        with (
            patch.object(lifecycle, "_FANOUT_CONCURRENCY", 2),
            patch.object(lifecycle, "_trigger_one", _instrumented),
            patch.object(
                lifecycle, "_list_tenants_for_action", AsyncMock(return_value=orgs)
            ),
            patch.object(
                lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
            ),
        ):
            await asyncio.gather(
                lifecycle.fanout_lifecycle_action("crystallize", auth=auth),
                lifecycle.fanout_lifecycle_action("insights", auth=auth),
            )
    finally:
        # The loop is session-scoped, so a budget of 2 left behind would be
        # inherited by every later fanout test. Drop it and let the next caller
        # rebuild one at the real value.
        if cache is not None:
            cache.clear()

    assert peak <= 2, (
        f"two concurrent fanouts reached {peak} simultaneous triggers against a "
        "budget of 2 — the cap is per-request again, so N actions on one cron "
        "minute still oversubscribe storage by N times"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fanout_tick_logs_the_dropped_org_count() -> None:
    """A partial sweep must not log as a clean one.

    core-api answers 200 with ``{"published", "failed"}`` even when orgs were
    dropped — one bad org must not abort the rest. The scheduler logged only
    ``published``, so on 2026-09-15 it reported success for every action while
    38 orgs went unprocessed. The count was in the response the whole time.
    """
    from core_operations import tasks

    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(
        return_value={"action": "crystallize", "published": 280, "failed": 20}
    )

    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(tasks.httpx, "AsyncClient", MagicMock(return_value=ctx)),
        patch.object(tasks.logger, "error") as err,
        patch.object(tasks.logger, "info") as info,
    ):
        await tasks._fire_fanout("crystallize")

    assert err.call_count == 1, "a sweep that dropped orgs must log at error"
    assert err.call_args.args[0] == "lifecycle fanout dropped orgs"
    assert err.call_args.kwargs["extra"]["failed"] == 20
    assert err.call_args.kwargs["extra"]["published"] == 280
    assert info.call_count == 0, "must not also report the sweep as fired"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fanout_tick_still_logs_a_clean_sweep_as_info() -> None:
    """The happy path keeps its info line, now carrying an explicit zero."""
    from core_operations import tasks

    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(
        return_value={"action": "crystallize", "published": 300, "failed": 0}
    )

    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(tasks.httpx, "AsyncClient", MagicMock(return_value=ctx)),
        patch.object(tasks.logger, "error") as err,
        patch.object(tasks.logger, "info") as info,
    ):
        await tasks._fire_fanout("crystallize")

    assert err.call_count == 0
    assert info.call_count == 1
    assert info.call_args.kwargs["extra"]["failed"] == 0


@pytest.mark.unit
def test_a_bad_concurrency_value_does_not_take_down_the_api() -> None:
    """A mistyped tunable must degrade to the default, not kill core-api.

    ``core_api.app`` imports this module unconditionally at startup
    (``app.py``: ``from core_api.routes.lifecycle import router``), so an
    exception raised at import time here stops EVERY route from serving —
    memories, search, auth — over one lifecycle cron knob.

    Reloading under a junk value is the only way to reach that: the parse
    happens at module scope, so exercising a function would not cover the
    failure mode this asserts against.
    """
    import importlib

    from core_api.routes import lifecycle

    try:
        with patch.dict(os.environ, {"LIFECYCLE_FANOUT_CONCURRENCY": "5o"}):
            importlib.reload(lifecycle)
        assert lifecycle._FANOUT_CONCURRENCY == 50, (
            "a junk value must fall back to the default, not propagate"
        )

        with patch.dict(os.environ, {"LIFECYCLE_FANOUT_CONCURRENCY": "0"}):
            importlib.reload(lifecycle)
        assert lifecycle._FANOUT_CONCURRENCY == 50, (
            "0 must fall back — Semaphore(0) is already locked and would park "
            "every fanout forever with no error and no timeout"
        )

        with patch.dict(os.environ, {"LIFECYCLE_FANOUT_CONCURRENCY": "12"}):
            importlib.reload(lifecycle)
        assert lifecycle._FANOUT_CONCURRENCY == 12, "a valid override must apply"
    finally:
        # Restore the module to its real configuration for every later test.
        os.environ.pop("LIFECYCLE_FANOUT_CONCURRENCY", None)
        importlib.reload(lifecycle)

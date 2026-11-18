"""The crystallizer's dedup sweep survives a batch that runs out of time.

Prod, nightly at 02:00 UTC from 2026-10-11: tenant ``arkash24-4d270c``'s first
500-row ``/memories/near-duplicates`` batch ran 300-890s on the primary
(770s on 2026-11-18, per Query Insights). core-api's storage read timeout and
the writer's Cloud Run request timeout both gave up at 120s, the batch ran on
for another ~11 minutes, and ``_check_near_duplicates`` raised before stamping
anything. The next night's first batch timed out the same way.

Two halves, tested here:

* storage caps the batch with ``statement_timeout``
  (``Settings.near_duplicates_statement_timeout_ms``) and answers 504;
* the sweep retries a timed-out window at ``CRYSTALLIZER_DEDUP_RETRY_BATCH_SIZE``,
  skips a window that times out even at that size, stops starting batches once
  ``CRYSTALLIZER_DEDUP_SWEEP_BUDGET_SECONDS`` is spent, and stamps whatever it
  swept.

The storage half needs a database like the rest of this tree.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text

import core_storage_api.services.postgres_service as ps
from core_api.constants import (
    CRYSTALLIZER_DEDUP_BATCH_SIZE,
    CRYSTALLIZER_DEDUP_RETRY_BATCH_SIZE,
    CRYSTALLIZER_DEDUP_SWEEP_BUDGET_SECONDS,
)
from core_api.services import crystallizer_service as cs
from core_storage_api.config import settings as storage_settings

_URL = "http://storage/api/v1/storage/memories/near-duplicates"


def _gateway_timeout() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", _URL)
    response = httpx.Response(504, request=request)
    return httpx.HTTPStatusError(
        "504 Gateway Timeout", request=request, response=response
    )


class _Storage:
    """``check_near_duplicates`` over ``rows`` unswept ids, no pairs.

    ``times_out(offset, batch_size)`` decides whether a batch runs out of time;
    ``error`` is what it raises then.
    """

    def __init__(self, rows: int, times_out, error=_gateway_timeout):
        self.ids = [f"m{i:05d}" for i in range(rows)]
        self.times_out = times_out
        self.error = error
        self.sizes: list[int] = []
        self.mark_dedup_checked = AsyncMock()

    async def check_near_duplicates(self, body: dict) -> dict:
        offset, size = body["offset"], body["batch_size"]
        self.sizes.append(size)
        if self.times_out(offset, size):
            raise self.error()
        return {"candidate_ids": self.ids[offset : offset + size], "pairs": []}

    def stamped(self) -> list[str]:
        return [
            mid
            for call in self.mark_dedup_checked.await_args_list
            for mid in call.args[0]
        ]


async def _sweep(monkeypatch, storage: _Storage) -> dict:
    monkeypatch.setattr(cs, "get_storage_client", lambda: storage)
    return await cs._check_near_duplicates("t1", None, threshold=0.95)


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [_gateway_timeout, lambda: httpx.ReadTimeout("timed out")],
    ids=["storage-504", "client-read-timeout"],
)
async def test_a_batch_that_times_out_is_retried_smaller_and_the_sweep_finishes(
    monkeypatch, error
):
    # Every row costs the same, so any batch over 60 rows runs out of time.
    storage = _Storage(300, lambda offset, size: size > 60, error)

    result = await _sweep(monkeypatch, storage)

    # One timeout, then the retry size to the end.
    assert storage.sizes[0] == CRYSTALLIZER_DEDUP_BATCH_SIZE
    assert set(storage.sizes[1:]) == {CRYSTALLIZER_DEDUP_RETRY_BATCH_SIZE}
    assert sorted(storage.stamped()) == storage.ids
    assert result["complete"] is True


@pytest.mark.unit
async def test_a_window_that_times_out_at_the_retry_size_is_skipped_not_fatal(
    monkeypatch,
):
    # Row 40 alone outlasts the timeout: every batch that contains it fails.
    storage = _Storage(300, lambda offset, size: offset <= 40 < offset + size)

    result = await _sweep(monkeypatch, storage)

    stamped = set(storage.stamped())
    unstamped = [mid for mid in storage.ids if mid not in stamped]
    # The one window holding row 40 is left for the next run, and the sweep
    # carries on past it to the end.
    assert "m00040" in unstamped
    assert len(unstamped) == CRYSTALLIZER_DEDUP_RETRY_BATCH_SIZE
    assert "m00299" in stamped
    assert result["complete"] is False


@pytest.mark.unit
async def test_the_budget_stops_the_sweep_and_keeps_what_it_swept(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    step = CRYSTALLIZER_DEDUP_SWEEP_BUDGET_SECONDS / 3
    monkeypatch.setattr(cs, "time", SimpleNamespace(monotonic=lambda: clock.now))
    storage = _Storage(5000, lambda offset, size: False)
    real_check = storage.check_near_duplicates

    async def slow_check(body: dict) -> dict:
        clock.now += step  # each batch takes a third of the budget
        return await real_check(body)

    storage.check_near_duplicates = slow_check

    result = await _sweep(monkeypatch, storage)

    assert len(storage.sizes) == 3
    assert sorted(storage.stamped()) == storage.ids[: 3 * CRYSTALLIZER_DEDUP_BATCH_SIZE]
    assert result["complete"] is False


@pytest.mark.unit
async def test_an_error_that_is_not_a_timeout_still_fails_the_check(monkeypatch):
    def server_error() -> httpx.HTTPStatusError:
        request = httpx.Request("POST", _URL)
        return httpx.HTTPStatusError(
            "500", request=request, response=httpx.Response(500, request=request)
        )

    storage = _Storage(300, lambda offset, size: True, server_error)

    with pytest.raises(httpx.HTTPStatusError):
        await _sweep(monkeypatch, storage)
    assert storage.sizes == [CRYSTALLIZER_DEDUP_BATCH_SIZE]
    storage.mark_dedup_checked.assert_not_awaited()


# ── storage: the batch's own statement_timeout ─────────────────────────────


def _wrap_session(monkeypatch, before_query):
    """Run ``before_query(session)`` ahead of each query the batch issues on
    its writer session, skipping its own ``set_config`` calls."""
    real_get_session = ps.get_session

    @contextlib.asynccontextmanager
    async def _wrapped():
        async with real_get_session() as session:

            class _Spy:
                async def execute(self, stmt, *args, **kwargs):
                    if "set_config" not in str(stmt):
                        await before_query(session)
                    return await session.execute(stmt, *args, **kwargs)

            yield _Spy()

    monkeypatch.setattr(ps, "get_session", _wrapped)


async def test_statement_timeout_is_live_when_the_batch_runs(
    tenant_id, monkeypatch
) -> None:
    monkeypatch.setattr(storage_settings, "near_duplicates_statement_timeout_ms", 1234)
    observed: list[str] = []

    async def _record(session):
        observed.append(
            (await session.execute(text("SHOW statement_timeout"))).scalar()
        )

    _wrap_session(monkeypatch, _record)
    rows = await ps.PostgresService().memory_find_near_duplicate_pairs(
        tenant_id, None, batch_size=10
    )
    assert rows == []  # empty tenant; the assertion is the GUC
    assert observed and set(observed) == {"1234ms"}, observed


async def test_zero_leaves_statement_timeout_unset(tenant_id, monkeypatch) -> None:
    monkeypatch.setattr(storage_settings, "near_duplicates_statement_timeout_ms", 0)
    observed: list[str] = []

    async def _record(session):
        observed.append(
            (await session.execute(text("SHOW statement_timeout"))).scalar()
        )

    _wrap_session(monkeypatch, _record)
    await ps.PostgresService().memory_find_near_duplicate_pairs(
        tenant_id, None, batch_size=10
    )
    assert observed and set(observed) == {"0"}, observed


async def test_a_batch_past_its_timeout_answers_504(
    storage_http, tenant_id, monkeypatch
) -> None:
    monkeypatch.setattr(storage_settings, "near_duplicates_statement_timeout_ms", 100)

    async def _stall(session):
        # Stands in for a batch slower than the cap.
        await session.execute(text("SELECT pg_sleep(1)"))

    _wrap_session(monkeypatch, _stall)
    r = await storage_http.post(
        "/api/v1/storage/memories/near-duplicates",
        json={"tenant_id": tenant_id, "batch_size": 10},
    )
    assert r.status_code == 504, r.text
    assert "statement timeout" in r.json()["detail"]

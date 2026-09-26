"""ax-0917-h-09 — a lost response left the idempotency key unrecorded.

An agent probe POSTed a document. It ran ~52s, the client lost the response,
and the server had already committed. The retry got 409 "still in progress";
a later replay with the same key performed a **second upsert**.

So the key failed in precisely the scenario it exists for: a lost response.

The write being durable was never the problem. `IdempotencyGuard.record` is
awaited on the request task, a client disconnect cancels that task, and
`CancelledError` is a `BaseException` — so the `except Exception` around the
storage call never saw it. The claim stayed unfinished (hence the 409), aged
out, and the replay was treated as a fresh request.

Shielding lets the record land even though the caller has gone. The caller
still observes `CancelledError` immediately, because the request really was
cancelled — same idiom as `storage_client._cancel_safe`.
"""

import asyncio
import inspect

import pytest

from core_api.middleware import idempotency

pytestmark = pytest.mark.unit


class _Recorder:
    """Storage double whose upsert takes long enough to be cancelled."""

    def __init__(self, delay: float = 0.05) -> None:
        self.calls: list[dict] = []
        self._delay = delay

    async def upsert_idempotency(self, **kw):
        await asyncio.sleep(self._delay)
        self.calls.append(kw)


def _ctx(monkeypatch, recorder):
    monkeypatch.setattr(idempotency, "get_storage_client", lambda: recorder)
    return idempotency.IdempotencyGuard(
        tenant_id="t1", key="k1", request_hash="h1", cached=None
    )


# ── the behaviour that was missing ───────────────────────────────────────


async def test_the_record_still_lands_when_the_caller_is_cancelled():
    """The whole bug. The server committed; the key must be recorded too, or
    the retry is unprotected."""
    rec = _Recorder()
    ctx = idempotency.IdempotencyGuard(
        tenant_id="t1", key="k1", request_hash="h1", cached=None
    )
    import core_api.middleware.idempotency as mod

    original = mod.get_storage_client
    mod.get_storage_client = lambda: rec
    try:
        task = asyncio.create_task(ctx.record({"id": "doc-1"}, 200))
        await asyncio.sleep(0)  # let it start and enter the shield
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The shielded write is still in flight — give it room to finish.
        await asyncio.sleep(0.2)
        assert rec.calls, "the idempotency record was lost on disconnect"
        assert rec.calls[0]["idempotency_key"] == "k1"
    finally:
        mod.get_storage_client = original


async def test_the_caller_still_sees_the_cancellation():
    """Shielding the record must not swallow the cancellation — the request
    really was cancelled, and pretending otherwise would hang the caller."""
    rec = _Recorder()
    import core_api.middleware.idempotency as mod

    original = mod.get_storage_client
    mod.get_storage_client = lambda: rec
    try:
        ctx = idempotency.IdempotencyGuard(
            tenant_id="t1", key="k1", request_hash="h1", cached=None
        )
        task = asyncio.create_task(ctx.record({"id": "doc-1"}, 200))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
    finally:
        mod.get_storage_client = original


async def test_an_ordinary_storage_failure_is_still_swallowed():
    """Unchanged: the client already has the live response, so losing the
    cache must not turn a successful write into an error."""

    class _Boom:
        async def upsert_idempotency(self, **kw):
            raise RuntimeError("storage down")

    import core_api.middleware.idempotency as mod

    original = mod.get_storage_client
    mod.get_storage_client = lambda: _Boom()
    try:
        ctx = idempotency.IdempotencyGuard(
            tenant_id="t1", key="k1", request_hash="h1", cached=None
        )
        await ctx.record({"id": "doc-1"}, 200)  # must not raise
    finally:
        mod.get_storage_client = original


async def test_the_happy_path_records_normally():
    rec = _Recorder(delay=0)
    import core_api.middleware.idempotency as mod

    original = mod.get_storage_client
    mod.get_storage_client = lambda: rec
    try:
        ctx = idempotency.IdempotencyGuard(
            tenant_id="t1", key="k1", request_hash="h1", cached=None
        )
        await ctx.record({"id": "doc-1"}, 201)
        assert rec.calls[0]["status_code"] == 201
    finally:
        mod.get_storage_client = original


# ── structure ────────────────────────────────────────────────────────────


def test_the_record_is_shielded():
    src = inspect.getsource(idempotency.IdempotencyGuard.record)
    assert "asyncio.shield" in src


def test_cancellation_is_re_raised_not_absorbed():
    src = inspect.getsource(idempotency.IdempotencyGuard.record)
    idx = src.index("except asyncio.CancelledError")
    assert "raise" in src[idx : idx + 400]


def test_an_orphaned_record_still_reports_its_outcome():
    """A shielded task that outlives its request and then raises would
    otherwise surface as 'exception was never retrieved', attributed to
    nothing."""
    src = inspect.getsource(idempotency.IdempotencyGuard.record)
    assert "add_done_callback" in src
    assert callable(idempotency._log_record_outcome)


def test_cancelled_is_checked_before_exception_on_the_callback():
    """``task.exception()`` RAISES on a cancelled task — checking cancellation
    second would turn the logger into the thing that throws."""
    src = inspect.getsource(idempotency._log_record_outcome)
    assert src.index("task.cancelled()") < src.index("task.exception()")

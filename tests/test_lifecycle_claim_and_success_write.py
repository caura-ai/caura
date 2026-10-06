"""Lifecycle claim and success writes (L-04, L-05).

L-04: ``_run_action`` caught any exception from the ``in_progress`` claim PATCH,
logged "continuing" and ran the primitive with no claim. Both adapters already
turn a 404 (a pruned row) into ``{}``, so that ``except`` only ever saw transport
and server errors, and on those the single-winner compare-and-swap was bypassed:
a slow storage plus a republished delivery ran the op twice. A failed claim
write now nacks. The worker's storage client also retried the PATCH only on
connection-phase failures, where core-api's retries timeouts and 5xx too; the
endpoint is idempotent under the claim token, so the worker now does the same.

L-05: the terminal success write was awaited bare, in ``_run_action`` and in the
embed-backfill handler. A storage blip after the op had finished nacked the
delivery, and the redelivery re-ran the whole op once the claim lease lapsed:
for crystallize or insights, a second LLM bill. The success write is now retried
in place under the claim's own token before the delivery is nacked. The
backfill handler also ignored its claim's answer, so a redelivery re-swept at
once and republished an embed request for every row still queued; it now
claims with a token and honours ``noop`` and ``claim_conflict`` as
``_run_action`` does.

M-09 and L-231: a shutdown that outlasts the bus's stop grace cancels the
handler. ``_run_action`` released its claim when cancelled mid-op, but not when
cancelled while its success write waited to retry, and the backfill handler
never did. Either way the row stayed claimed for the lease, and its redelivery
nacked on the claim until then.
"""

from __future__ import annotations

import asyncio
import contextlib
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from common.events import lifecycle_handlers
from common.events.base import Event
from common.events.lifecycle_archive_request import LifecycleArchiveRequest
from common.events.topics import Topics
from core_worker import consumer
from core_worker.clients import storage_client as worker_storage

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    monkeypatch.setattr(
        lifecycle_handlers,
        "_SUCCESS_WRITE_RETRY_DELAYS_SECONDS",
        (0.0, 0.0, 0.0),
        raising=False,
    )


# ── _run_action ───────────────────────────────────────────────────────────


class _Adapter:
    """Answers the audit writes from a script, and counts the primitive's runs."""

    def __init__(self, *, claim=None, success_errors=()):
        self.claim = claim
        self.success_errors = list(success_errors)
        self.writes: list[tuple[str, str | None]] = []
        self.runs = 0

    async def update_lifecycle_audit_row(
        self,
        audit_id,
        *,
        org_id,
        status,
        stats=None,
        error_message=None,
        claim_token=None,
    ):
        self.writes.append((status, claim_token))
        if status == "in_progress" and isinstance(self.claim, BaseException):
            raise self.claim
        if status == "in_progress":
            return self.claim or {}
        if status == "success" and self.success_errors:
            raise self.success_errors.pop(0)
        return {}

    async def archive_expired(self, *, org_id, fleet_id):
        self.runs += 1
        return 3


def _handler(adapter: _Adapter):
    async def op(req: LifecycleArchiveRequest) -> int:
        return await adapter.archive_expired(org_id=req.org_id, fleet_id=req.fleet_id)

    return partial(
        lifecycle_handlers._run_action,
        adapter=adapter,
        payload_cls=LifecycleArchiveRequest,
        run_op=op,
        stats_key="archived",
        action="archive-expired",
    )


def _event() -> Event:
    payload = LifecycleArchiveRequest(
        audit_id=42, org_id="tenant-x", triggered_by="test"
    ).model_dump(mode="json")
    return Event(event_type=Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED, payload=payload)


def _statuses(adapter: _Adapter) -> list[str]:
    return [status for status, _ in adapter.writes]


async def test_a_failed_claim_write_nacks_instead_of_running_unclaimed():
    adapter = _Adapter(claim=httpx.ReadTimeout("storage is slow"))
    with pytest.raises(httpx.ReadTimeout):
        await _handler(adapter)(_event())
    assert adapter.runs == 0
    assert _statuses(adapter) == ["in_progress"]


async def test_a_pruned_audit_row_still_runs_the_op():
    """Guard: a 404 reaches the handler as ``{}``, and the op must still run."""
    adapter = _Adapter(claim={})
    await _handler(adapter)(_event())
    assert adapter.runs == 1
    assert _statuses(adapter) == ["in_progress", "success"]


async def test_a_flaky_success_write_is_retried_in_place_under_the_claim():
    adapter = _Adapter(
        success_errors=[httpx.ReadTimeout("lost"), RuntimeError("storage 503")]
    )
    await _handler(adapter)(_event())
    assert adapter.runs == 1
    assert _statuses(adapter) == ["in_progress", "success", "success", "success"]
    tokens = {token for _, token in adapter.writes}
    assert len(tokens) == 1 and None not in tokens


async def test_a_success_write_that_keeps_failing_still_nacks_once_retries_run_out():
    adapter = _Adapter(success_errors=[RuntimeError("storage down")] * 10)
    with pytest.raises(RuntimeError, match="storage down"):
        await _handler(adapter)(_event())
    assert adapter.runs == 1
    assert _statuses(adapter).count("success") == 4


# ── the worker's storage client ───────────────────────────────────────────


def _response(status: int, body: object = None) -> MagicMock:
    resp = MagicMock(status_code=status)
    error = httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)
    resp.raise_for_status = MagicMock(side_effect=error if status >= 400 else None)
    resp.json = MagicMock(return_value=body)
    return resp


@pytest.mark.parametrize(
    "first",
    [httpx.ReadTimeout("response lost"), _response(503)],
    ids=["read-timeout", "503"],
)
async def test_the_worker_retries_a_lifecycle_audit_write_in_place(monkeypatch, first):
    monkeypatch.setattr(worker_storage, "_audience", None)
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(side_effect=[first, _response(200, {"ok": True})])

    out = await worker_storage.update_lifecycle_audit_row(
        client, 42, org_id="t1", status="in_progress", claim_token="tok"
    )

    assert out == {"ok": True}
    assert client.patch.await_count == 2


# ── the embed-backfill handler ────────────────────────────────────────────


def _backfill_event() -> Event:
    return Event(
        event_type=Topics.Lifecycle.EMBED_BACKFILL_REQUESTED,
        tenant_id="org-42",
        payload={"audit_id": 7, "org_id": "org-42", "triggered_by": "core-operations"},
    )


@contextlib.contextmanager
def _backfill(writes: list):
    """The audit writes answer from ``writes``, in order; yields (sweep, audit)."""
    report = MagicMock(scanned=2, published=2, skipped_missing=0, elapsed_s=0.1)
    with (
        patch.object(
            consumer, "run_embedding_backfill", AsyncMock(return_value=report)
        ) as sweep,
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(
            consumer, "update_lifecycle_audit_row", AsyncMock(side_effect=writes)
        ) as audit,
    ):
        yield sweep, audit


def _backfill_statuses(audit: AsyncMock) -> list[str]:
    return [c.kwargs["status"] for c in audit.await_args_list]


async def test_the_backfill_does_not_sweep_a_row_another_consumer_holds():
    with _backfill([{"claim_conflict": True}, {}]) as (sweep, audit):
        with pytest.raises(RuntimeError, match="claimed by another consumer"):
            await consumer.handle_embed_backfill_request(_backfill_event())
    sweep.assert_not_awaited()
    assert _backfill_statuses(audit) == ["in_progress"]


async def test_a_backfill_redelivery_of_a_finished_sweep_does_not_sweep_again():
    with _backfill([{"noop": True}, {}]) as (sweep, audit):
        await consumer.handle_embed_backfill_request(_backfill_event())
    sweep.assert_not_awaited()
    assert _backfill_statuses(audit) == ["in_progress"]


async def test_the_backfill_claims_and_finalises_under_one_token():
    with _backfill([{}, {}]) as (sweep, audit):
        await consumer.handle_embed_backfill_request(_backfill_event())
    sweep.assert_awaited_once()
    tokens = [c.kwargs.get("claim_token") for c in audit.await_args_list]
    assert _backfill_statuses(audit) == ["in_progress", "success"]
    assert tokens[0] is not None and tokens[0] == tokens[1]


async def test_the_backfill_retries_a_flaky_success_write_in_place():
    with _backfill([{}, httpx.ReadTimeout("lost"), {}]) as (sweep, audit):
        await consumer.handle_embed_backfill_request(_backfill_event())
    sweep.assert_awaited_once()
    assert _backfill_statuses(audit) == ["in_progress", "success", "success"]


# ── a cancel on the way out (M-09, L-231) ─────────────────────────────────


async def test_a_cancel_while_the_success_write_waits_still_records_the_success(
    monkeypatch,
):
    """The op has run and its success write is waiting to retry when shutdown
    cancels the handler. One bounded write records the success under the claim
    before the cancellation goes on, so the redelivery acks instead of running
    the op again."""
    monkeypatch.setattr(
        lifecycle_handlers, "_SUCCESS_WRITE_RETRY_DELAYS_SECONDS", (60.0,)
    )
    adapter = _Adapter(success_errors=[RuntimeError("storage 503")])
    task = asyncio.create_task(_handler(adapter)(_event()))
    for _ in range(200):
        if len(adapter.writes) == 2:
            break
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.runs == 1
    assert _statuses(adapter) == ["in_progress", "success", "success"]
    assert len({token for _, token in adapter.writes}) == 1


async def test_a_backfill_cancelled_mid_sweep_releases_its_claim():
    with _backfill([{}, {}]) as (sweep, audit):
        sweep.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await consumer.handle_embed_backfill_request(_backfill_event())
    assert _backfill_statuses(audit) == ["in_progress", "failure"]
    tokens = [c.kwargs.get("claim_token") for c in audit.await_args_list]
    assert tokens[0] is not None and tokens[0] == tokens[1]
    assert "cancelled" in audit.await_args_list[1].kwargs["error_message"]

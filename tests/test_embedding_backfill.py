"""Unit tests for the event-driven embedding backfill task.

The task lives at ``core-worker/src/core_worker/backfill.py`` and drives
the existing ``handle_embed_request`` consumer by publishing one
``EMBED_REQUESTED`` event per memory whose ``embedding IS NULL``.

Mocks the storage-client iterator + the per-row ``get_memory`` fetch
+ the embed-request publisher. No real DB or event bus. Integration
coverage (against staging Postgres + a Pub/Sub emulator) is covered
by the staging cutover runbook (Spec E), not this PR.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core_worker.backfill import run_embedding_backfill
from core_worker.clients.storage_client import NullEmbeddingRow


def _row() -> NullEmbeddingRow:
    return NullEmbeddingRow(memory_id=uuid.uuid4(), tenant_id="tenant-A")


def _make_get_memory(
    content: str = "hello", content_hash: str | None = None
) -> AsyncMock:
    """Build an ``AsyncMock`` for ``get_memory`` that returns a fake
    memory dict matching the storage API's shape."""
    return AsyncMock(return_value={"content": content, "content_hash": content_hash})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_publishes_one_event_per_null_row() -> None:
    """Happy path: every NULL row → one ``get_memory`` fetch + one
    ``publish_memory_embed_request`` call."""
    rows = [_row() for _ in range(7)]

    async def _fake_iter(_storage, **_kw):
        # Two pages: 5 + 2.
        yield rows[:5]
        yield rows[5:]

    publish = AsyncMock()
    get_memory = _make_get_memory()
    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        report = await run_embedding_backfill(
            tenant_id="t-1", batch_size=5, max_inflight=2
        )

    assert report.scanned == 7
    assert report.published == 7
    assert report.skipped_missing == 0
    assert get_memory.await_count == 7
    assert publish.await_count == 7
    sample = publish.await_args_list[0].kwargs
    assert {"memory_id", "tenant_id", "content", "content_hash"} <= set(sample)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_dry_run_fetches_content_but_does_not_publish() -> None:
    """``--dry-run`` still does the per-row content fetch (so the report
    accurately accounts for soft-deleted rows) but skips the publish."""
    rows = [_row() for _ in range(3)]

    async def _fake_iter(_storage, **_kw):
        yield rows

    publish = AsyncMock()
    get_memory = _make_get_memory()
    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        report = await run_embedding_backfill(tenant_id="t-1", dry_run=True)

    assert report.scanned == 3
    assert report.published == 3  # counted as "would have"
    assert get_memory.await_count == 3
    assert publish.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_no_null_rows_returns_zero_report() -> None:
    """No NULL rows → empty report, no fetches, no publishes, clean exit."""

    async def _fake_iter(_storage, **_kw):
        if False:  # generator with no yields
            yield

    publish = AsyncMock()
    get_memory = _make_get_memory()
    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        report = await run_embedding_backfill(tenant_id="t-1")

    assert report.scanned == 0
    assert report.published == 0
    assert get_memory.await_count == 0
    assert publish.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_passes_tenant_filter_to_iterator() -> None:
    """``tenant_id`` and ``batch_size`` reach the storage iterator."""
    captured: dict = {}

    async def _fake_iter(_storage, **kw):
        captured.update(kw)
        if False:
            yield

    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", AsyncMock()),
        patch("core_worker.backfill.get_memory", _make_get_memory()),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        await run_embedding_backfill(tenant_id="tenant-A", batch_size=42)

    assert captured["tenant_id"] == "tenant-A"
    assert captured["batch_size"] == 42


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_publishes_full_embed_request_payload() -> None:
    """Each publish call receives content + hash + tenant — fetched
    per-row from ``get_memory`` after the listing endpoint hands the
    worker the id. Guards against regressing to a partial payload that
    the consumer's Pydantic model would reject and burn the DLQ budget on."""
    row = NullEmbeddingRow(memory_id=uuid.uuid4(), tenant_id="tenant-X")

    async def _fake_iter(_storage, **_kw):
        yield [row]

    publish = AsyncMock()
    get_memory = _make_get_memory(content="some memory body", content_hash="hash-abc")
    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        await run_embedding_backfill(tenant_id="t-1")

    get_memory.assert_awaited_once_with(
        get_memory.await_args.args[0],  # the storage client
        memory_id=row.memory_id,
        tenant_id="tenant-X",
    )
    publish.assert_awaited_once_with(
        memory_id=row.memory_id,
        content="some memory body",
        tenant_id="tenant-X",
        content_hash="hash-abc",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_skips_404_between_listing_and_fetch() -> None:
    """A row that the listing endpoint reported but ``get_memory``
    can't find (soft-/hard-deleted in the gap) is counted as
    ``skipped_missing`` rather than failing the whole backfill."""
    rows = [_row() for _ in range(3)]

    async def _fake_iter(_storage, **_kw):
        yield rows

    publish = AsyncMock()

    # Build a 404 HTTPStatusError for one of the three rows.
    not_found = httpx.HTTPStatusError(
        "404",
        request=httpx.Request("GET", "http://x/memories/y"),
        response=httpx.Response(status_code=404),
    )
    get_memory = AsyncMock(
        side_effect=[
            {"content": "first", "content_hash": None},
            not_found,
            {"content": "third", "content_hash": None},
        ]
    )

    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", publish),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        report = await run_embedding_backfill(tenant_id="t-1")

    assert report.scanned == 3
    assert report.published == 2
    assert report.skipped_missing == 1
    assert publish.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_propagates_non_404_http_errors() -> None:
    """A 500 from the storage API is NOT silently absorbed — the
    backfill aborts so the operator notices instead of a silent
    long-running no-op."""
    rows = [_row()]

    async def _fake_iter(_storage, **_kw):
        yield rows

    server_error = httpx.HTTPStatusError(
        "500",
        request=httpx.Request("GET", "http://x/memories/y"),
        response=httpx.Response(status_code=500),
    )
    get_memory = AsyncMock(side_effect=server_error)

    with (
        patch("core_worker.backfill.iter_memories_with_null_embedding", _fake_iter),
        patch("core_worker.backfill.publish_memory_embed_request", AsyncMock()),
        patch("core_worker.backfill.get_memory", get_memory),
        patch("core_worker.backfill.get_storage_client", return_value=MagicMock()),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await run_embedding_backfill(tenant_id="t-1")


# ---------------------------------------------------------------------------
# Scheduled sweep — the consumer that drives the backfill per org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_backfill_handler_sweeps_the_orgs_tenant() -> None:
    """The lifecycle event's ``org_id`` is what gets swept.

    Registered in core-worker rather than routed through the shared lifecycle
    adapter because ``run_embedding_backfill`` lives here and core-api — which
    implements the same adapter protocol — cannot run it.
    """
    from common.events.base import Event
    from common.events.topics import Topics
    from core_worker import consumer

    event = Event(
        event_type=Topics.Lifecycle.EMBED_BACKFILL_REQUESTED,
        tenant_id="org-42",
        payload={"audit_id": 7, "org_id": "org-42", "triggered_by": "core-operations"},
    )
    report = MagicMock(scanned=9, published=8, skipped_missing=1, elapsed_s=1.25)
    with (
        patch.object(
            consumer, "run_embedding_backfill", AsyncMock(return_value=report)
        ) as swept,
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(consumer, "update_lifecycle_audit_row", AsyncMock()),
    ):
        await consumer.handle_embed_backfill_request(event)

    swept.assert_awaited_once()
    assert swept.await_args.kwargs["tenant_id"] == "org-42"
    # Conservative publish cap, env-tunable so a sweep competing with live
    # load can be throttled without a deploy. Must not enqueue at the
    # library default of 100.
    from core_worker.config import settings

    assert swept.await_args.kwargs["max_inflight"] == settings.embed_backfill_max_inflight
    assert settings.embed_backfill_max_inflight < 100


@pytest.mark.asyncio
async def test_embed_backfill_handler_logs_the_counts(caplog) -> None:
    """Emit per-org counts — before this, "how many rows are unembedded" was
    only answerable by querying AlloyDB directly, because the coverage
    endpoint is internal-ingress and no metric carried it."""
    import logging

    from common.events.base import Event
    from common.events.topics import Topics
    from core_worker import consumer

    event = Event(
        event_type=Topics.Lifecycle.EMBED_BACKFILL_REQUESTED,
        tenant_id="org-42",
        payload={"audit_id": 7, "org_id": "org-42", "triggered_by": "core-operations"},
    )
    report = MagicMock(scanned=9, published=8, skipped_missing=1, elapsed_s=1.25)
    with (
        caplog.at_level(logging.INFO, logger="core_worker.consumer"),
        patch.object(consumer, "run_embedding_backfill", AsyncMock(return_value=report)),
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(consumer, "update_lifecycle_audit_row", AsyncMock()),
    ):
        await consumer.handle_embed_backfill_request(event)

    rec = next(r for r in caplog.records if r.getMessage() == "embed-backfill sweep processed")
    assert rec.org_id == "org-42"
    assert rec.scanned == 9
    assert rec.published == 8
    assert rec.skipped_missing == 1


@pytest.mark.asyncio
async def test_embed_backfill_handler_ack_drops_malformed_payload() -> None:
    """A malformed payload is permanent, so ack-drop rather than raise.

    Raising would nack and redeliver the same poison message until the DLQ
    took it — the same guard ``handle_embed_request`` already has.
    """
    from common.events.base import Event
    from common.events.topics import Topics
    from core_worker import consumer

    event = Event(
        event_type=Topics.Lifecycle.EMBED_BACKFILL_REQUESTED,
        tenant_id="org-42",
        payload={"not": "a lifecycle request"},
    )
    with patch.object(consumer, "run_embedding_backfill", AsyncMock()) as swept:
        await consumer.handle_embed_backfill_request(event)  # must not raise

    swept.assert_not_awaited()


def test_embed_backfill_topic_is_subscribed() -> None:
    """Registration must happen in ``register_consumers``.

    The Pub/Sub backend spawns its pull loops in ``start()`` from the handler
    registry as it stands then, so a handler defined but never subscribed is
    silently orphaned.
    """
    import inspect

    from core_worker import consumer

    source = inspect.getsource(consumer.register_consumers)
    assert "EMBED_BACKFILL_REQUESTED" in source
    assert "handle_embed_backfill_request" in source


def _backfill_event():
    from common.events.base import Event
    from common.events.topics import Topics

    return Event(
        event_type=Topics.Lifecycle.EMBED_BACKFILL_REQUESTED,
        tenant_id="org-42",
        payload={"audit_id": 7, "org_id": "org-42", "triggered_by": "core-operations"},
    )


@pytest.mark.asyncio
async def test_embed_backfill_handler_finalises_the_audit_row() -> None:
    """The fanout pre-creates the row as ``pending`` on purpose, so that a row
    which never advances reads as a publish failure. A successful sweep that
    left it pending would be indistinguishable from an undelivered message."""
    from core_worker import consumer

    report = MagicMock(scanned=9, published=8, skipped_missing=1, elapsed_s=1.25)
    with (
        patch.object(consumer, "run_embedding_backfill", AsyncMock(return_value=report)),
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(consumer, "update_lifecycle_audit_row", AsyncMock()) as audit,
    ):
        await consumer.handle_embed_backfill_request(_backfill_event())

    # Two PATCHes: in_progress on entry, then the finalising one asserted here.
    assert audit.await_count == 2, [c.kwargs for c in audit.await_args_list]
    final = audit.await_args_list[-1]
    assert final.args[1] == 7, "must patch the audit_id from the payload"
    from core_storage_api.routers.lifecycle_audit import _VALID_STATUSES

    assert final.kwargs["status"] in _VALID_STATUSES
    assert final.kwargs["status"] == "success"
    assert final.kwargs["stats"] == {
        "scanned": 9,
        "published": 8,
        "skipped_missing": 1,
    }


@pytest.mark.asyncio
async def test_embed_backfill_handler_marks_audit_failed_then_reraises() -> None:
    """On a sweep failure the row must say ``failure`` — and the exception must
    still propagate so Pub/Sub redelivers. Recording the outcome and retrying
    are both wanted; swallowing to write the row would lose the retry."""
    from core_worker import consumer

    boom = RuntimeError("storage exploded")
    with (
        patch.object(consumer, "run_embedding_backfill", AsyncMock(side_effect=boom)),
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(consumer, "update_lifecycle_audit_row", AsyncMock()) as audit,
    ):
        with pytest.raises(RuntimeError, match="storage exploded"):
            await consumer.handle_embed_backfill_request(_backfill_event())

    # Pinned against the route's own allow-list, not a literal. The first draft
    # of this test asserted ``"failed"`` — the same wrong string the handler
    # used — so it passed while the real PATCH would have 422'd, masked the
    # original error, and left the row pending. Asserting my own value proved
    # nothing; asserting the contract does.
    from core_storage_api.routers.lifecycle_audit import _VALID_STATUSES

    assert audit.await_count == 2, [c.kwargs for c in audit.await_args_list]
    final = audit.await_args_list[-1]
    status = final.kwargs["status"]
    assert status in _VALID_STATUSES, f"{status!r} would 422; valid: {sorted(_VALID_STATUSES)}"
    assert status == "failure"
    assert "storage exploded" in final.kwargs["error_message"]


@pytest.mark.unit
def test_embed_backfill_is_a_registered_lifecycle_action() -> None:
    """The fanout route must know the action, or the nightly tick 404s.

    core-operations POSTs ``/admin/lifecycle/fanout/embed-backfill``; an
    unregistered action returns 404 from ``_resolve_publisher`` and the tick
    logs-and-returns, so the sweep would silently never run.
    """
    from common.events.lifecycle_publishers import publish_embed_backfill_request
    from core_api.routes.lifecycle import _ACTION_PUBLISHERS

    assert _ACTION_PUBLISHERS["embed-backfill"] is publish_embed_backfill_request


@pytest.mark.unit
def test_embed_backfill_topic_string() -> None:
    """Pinned like its siblings — the wire name is infra's contract too.

    The topic and its durable subscription are Terraform-provisioned (the bus
    only auto-creates broadcast subscriptions), so renaming this constant
    silently detaches the publisher from the provisioned topic.
    """
    from common.events.topics import Topics

    assert (
        Topics.Lifecycle.EMBED_BACKFILL_REQUESTED
        == "memclaw.lifecycle.embed-backfill-requested"
    )


@pytest.mark.asyncio
async def test_embed_backfill_handler_refuses_a_fleet_scoped_request() -> None:
    """A fleet-scoped request must fail loudly, not sweep the whole org.

    ``fleet_id`` is a real field on the shared lifecycle payload and the manual
    trigger route forwards it, but nothing on this path can honour it:
    ``run_embedding_backfill`` takes no fleet parameter and
    ``GET /memories/null-embedding-ids`` filters by tenant only. Silently
    proceeding would re-embed the entire org for an operator who asked for one
    fleet — exceeding the requested blast radius on a write-generating path.
    The scheduled fanout never sets ``fleet_id``, so only deliberate manual
    triggers reach this.
    """
    from common.events.base import Event
    from common.events.topics import Topics
    from core_storage_api.routers.lifecycle_audit import _VALID_STATUSES
    from core_worker import consumer

    event = Event(
        event_type=Topics.Lifecycle.EMBED_BACKFILL_REQUESTED,
        tenant_id="org-42",
        payload={
            "audit_id": 7,
            "org_id": "org-42",
            "triggered_by": "operator",
            "fleet_id": "fleet-a",
        },
    )
    with (
        patch.object(consumer, "run_embedding_backfill", AsyncMock()) as swept,
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(consumer, "update_lifecycle_audit_row", AsyncMock()) as audit,
    ):
        await consumer.handle_embed_backfill_request(event)

    swept.assert_not_awaited(), "must not sweep wider than requested"
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["status"] in _VALID_STATUSES
    assert audit.await_args.kwargs["status"] == "failure"
    assert "fleet" in audit.await_args.kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_embed_backfill_audit_failure_does_not_mask_the_real_error() -> None:
    """A bookkeeping failure must not replace the cause.

    An unguarded PATCH in the except-block would raise its own exception in
    place of the sweep's, losing the real cause while still leaving the row
    unfinalised — the same ambiguity the finalisation exists to remove.
    """
    from core_worker import consumer

    boom = RuntimeError("the real cause")
    with (
        patch.object(consumer, "run_embedding_backfill", AsyncMock(side_effect=boom)),
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(
            consumer,
            "update_lifecycle_audit_row",
            AsyncMock(side_effect=[None, RuntimeError("storage blip")]),
        ),
    ):
        # The sweep's error propagates, not the PATCH's.
        with pytest.raises(RuntimeError, match="the real cause"):
            await consumer.handle_embed_backfill_request(_backfill_event())


@pytest.mark.asyncio
async def test_embed_backfill_marks_in_progress_before_sweeping() -> None:
    """Mid-run, the row must read ``in_progress``, not the fanout's ``pending``.

    A per-org sweep is not instant, and without this transition an operator
    cannot tell "actively sweeping" from "never picked up". Matches the shared
    ``_run_action``.
    """
    from core_storage_api.routers.lifecycle_audit import _VALID_STATUSES
    from core_worker import consumer

    report = MagicMock(scanned=9, published=8, skipped_missing=1, elapsed_s=1.25)
    with (
        patch.object(consumer, "run_embedding_backfill", AsyncMock(return_value=report)),
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(consumer, "update_lifecycle_audit_row", AsyncMock()) as audit,
    ):
        await consumer.handle_embed_backfill_request(_backfill_event())

    statuses = [c.kwargs["status"] for c in audit.await_args_list]
    assert statuses == ["in_progress", "success"], statuses
    assert all(s in _VALID_STATUSES for s in statuses)


def test_embed_backfill_inflight_cap_rejects_zero() -> None:
    """0 would make the publish semaphore block forever — a hang, not an error.

    Same trap the per-tenant storage cap guards against, so it is rejected at
    config load rather than discovered as a sweep that never finishes.
    """
    import pytest as _pytest
    from pydantic import ValidationError

    from core_worker.config import Settings

    with _pytest.raises(ValidationError, match="must be >= 1"):
        Settings(embed_backfill_max_inflight=0)


@pytest.mark.asyncio
async def test_embed_backfill_sweeps_even_if_in_progress_update_fails() -> None:
    """Bookkeeping must not decide whether the work happens.

    ``_run_action`` marks ``in_progress`` best-effort for this reason: letting
    it raise would nack before the sweep started, skipping an op the operator
    asked for because a status write failed. The sweep is idempotent, so
    running it against a stale row beats not running it.
    """
    from core_worker import consumer

    report = MagicMock(scanned=3, published=3, skipped_missing=0, elapsed_s=0.5)
    with (
        patch.object(consumer, "run_embedding_backfill", AsyncMock(return_value=report)) as swept,
        patch.object(consumer, "get_storage_client", MagicMock()),
        patch.object(
            consumer,
            "update_lifecycle_audit_row",
            # in_progress raises; the finalising call succeeds.
            AsyncMock(side_effect=[RuntimeError("status write blip"), None]),
        ),
    ):
        await consumer.handle_embed_backfill_request(_backfill_event())

    swept.assert_awaited_once(), "a failed status write must not skip the sweep"

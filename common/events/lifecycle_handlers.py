"""Consumers for ``memclaw.lifecycle.<action>-requested`` topics
(CAURA-655 archive ops, CAURA-656 purge, CAURA-657 pipeline ops).

Lives in ``common/`` rather than under either service so the same code
runs in both deployments. Two registration entry points reflect the
split between SQL-only and pipeline-machinery ops:

* :func:`register_archive_consumers` — archive + purge ops. Subscriber
  is core-worker on SaaS; in OSS standalone core-api also subscribes
  (no separate worker process).
* :func:`register_pipeline_consumers` — crystallize + entity-link.
  Subscriber is ALWAYS core-api because the consumer needs core-api's
  pipeline machinery (run_crystallization, build_full_entity_linking_pipeline).

The handler delegates the storage round-trips it needs (run the
primitive, finalise the audit row, optionally check the dedup gate)
to a small adapter the host service supplies. Per-action ops bind
their own primitive callable + payload class at registration time so
the dispatch never branches on a string.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol

from pydantic import ValidationError

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.lifecycle_archive_request import (
    LifecycleArchiveRequest,
    LifecycleRequestBase,
)
from common.events.lifecycle_purge_request import LifecyclePurgeRequest
from common.events.topics import Topics

logger = logging.getLogger(__name__)


class ArchiveStorageAdapter(Protocol):
    """Methods the SQL-only lifecycle consumers (archive + purge) need."""

    async def archive_expired(self, *, org_id: str, fleet_id: str | None) -> int: ...

    async def archive_stale(self, *, org_id: str, fleet_id: str | None) -> int: ...

    async def purge_soft_deleted(
        self, *, org_id: str, fleet_id: str | None, retention_days: int
    ) -> int: ...

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None: ...


class PipelineStorageAdapter(Protocol):
    """Methods the LLM-heavy lifecycle consumers (crystallize +
    entity-link) need. Wider than :class:`ArchiveStorageAdapter` —
    adds the dedup gate (``has_recent_lifecycle_success``) plus the
    two pipeline primitives. Both consumers live in core-api because
    they need its pipeline machinery; this protocol exists so the
    handler module stays free of core-api imports.
    """

    async def crystallize(self, *, org_id: str, fleet_id: str | None) -> int: ...

    async def entity_link(self, *, org_id: str, fleet_id: str | None) -> int: ...

    async def insights(self, *, org_id: str, fleet_id: str | None) -> int: ...

    async def has_recent_lifecycle_success(
        self, *, org_id: str, action: str, since_hours: int
    ) -> bool: ...

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None: ...


# Back-compat alias so existing core-api/core-worker adapter code that
# imports ``LifecycleStorageAdapter`` keeps type-checking. The shared
# object on both sides actually implements both protocols today; the
# split is at the registration boundary, not the adapter side.
LifecycleStorageAdapter = ArchiveStorageAdapter


# Pipeline ops dedup window. Cron fires daily, so 23 hours catches
# "fired twice within an hour due to a redeploy" while still letting
# the legitimate next-day tick through.
_PIPELINE_DEDUP_WINDOW_HOURS = 23


# Callable parameters are contravariant: an op that accepts a
# ``LifecycleArchiveRequest`` (subclass) is NOT assignable to a
# ``Callable[[LifecycleRequestBase], ...]``. Use ``...`` so the
# registered closures (each with its own subclass-typed argument)
# satisfy the alias without a ``# type: ignore``. ``_run_action``
# never inspects ``run_op``'s parameter type itself — the caller
# always passes the correctly-shaped request — so the looser alias
# carries no runtime risk.
_OpFn = Callable[..., Awaitable[int]]


async def _run_action(
    event: Event,
    *,
    adapter: ArchiveStorageAdapter | PipelineStorageAdapter,
    payload_cls: type[LifecycleRequestBase],
    run_op: _OpFn,
    stats_key: str,
    action: str,
    dedup_window_hours: int | None = None,
) -> None:
    """Shared body for every lifecycle action — bound to a specific
    primitive at registration time so this function never branches on
    a string. SQL ops are naturally idempotent so Pub/Sub redelivery
    is safe; each delivery attempt updates the SAME audit row (the
    row id rides in the payload, pre-created by the fanout endpoint).

    ``dedup_window_hours`` is set for pipeline ops only (CAURA-657):
    if a successful run for the same org+action exists within the
    window, this delivery is a no-op (audit row marked success with
    ``stats.skipped`` so observers can distinguish "did the work" from
    "skipped because already done"). Filtering on ``finished_at``
    naturally excludes the in-progress row pre-published moments ago.
    """
    try:
        request = payload_cls(**event.payload)
    except ValidationError:
        logger.exception(
            "dropping malformed lifecycle-request payload",
            extra={
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "dropped": True,
            },
        )
        return

    audit_id = request.audit_id
    org_id = request.org_id
    triggered_by = request.triggered_by

    # Dedup gate (pipeline ops only). Runs BEFORE the in_progress mark
    # so the audit row's life cycle stays clean — pending → success
    # with skipped=true, no in_progress flicker.
    if dedup_window_hours is not None:
        try:
            already_done = await adapter.has_recent_lifecycle_success(  # type: ignore[union-attr]
                org_id=org_id, action=action, since_hours=dedup_window_hours
            )
        except Exception:
            # Failed dedup check shouldn't block the op — better to run
            # twice than skip a legitimate request because the gate
            # endpoint flaked. Log and proceed.
            logger.warning(
                "lifecycle dedup check failed; proceeding without skip",
                exc_info=True,
                extra={"audit_id": audit_id, "action": action},
            )
            already_done = False
        if already_done:
            # Same best-effort treatment as the in_progress mark
            # below: the skip record is observability data, and a
            # raise here would nack the message into a redeliver
            # loop that re-checks the gate, fails the same write,
            # and eventually DLQs a legitimate skip.
            try:
                await adapter.update_lifecycle_audit_row(
                    audit_id,
                    status="success",
                    stats={"skipped": True, "reason": "recent_success"},
                )
            except Exception:
                logger.warning(
                    "lifecycle audit skip update failed; acking anyway",
                    exc_info=True,
                    extra={"audit_id": audit_id, "action": action},
                )
            logger.info(
                "lifecycle %s skipped — recent successful run exists",
                action,
                extra={
                    "audit_id": audit_id,
                    "org_id": org_id,
                    "triggered_by": triggered_by,
                },
            )
            return

    # Best-effort in_progress mark: 404 means the audit row was pruned
    # between fanout and consume. Continue anyway so the primitive
    # still runs — dropping would silently skip an op the operator
    # asked for.
    try:
        await adapter.update_lifecycle_audit_row(audit_id, status="in_progress")
    except Exception:
        logger.warning(
            "lifecycle audit in_progress update failed; continuing",
            exc_info=True,
            extra={"audit_id": audit_id, "action": action},
        )

    try:
        count = await run_op(request)
    except Exception as exc:
        # Wrap the failure update in its own guard: if it raises, the
        # outer ``raise`` below would never run and the original op
        # exception would be silently replaced by the audit error,
        # leaving the row stuck in ``in_progress`` indistinguishable
        # from a crashed worker.
        try:
            await adapter.update_lifecycle_audit_row(
                audit_id,
                status="failure",
                error_message=str(exc)[:500],
            )
        except Exception:
            logger.warning(
                "lifecycle audit failure update failed; row stuck in_progress",
                exc_info=True,
                extra={"audit_id": audit_id, "action": action},
            )
        # Re-raise so the bus nacks → Pub/Sub redelivers (subject to
        # max-delivery-attempts → DLQ). The ``failure`` row above is
        # the durable record (when the update succeeded).
        raise

    await adapter.update_lifecycle_audit_row(
        audit_id,
        status="success",
        stats={stats_key: count},
    )

    logger.info(
        "lifecycle %s processed",
        action,
        extra={
            "audit_id": audit_id,
            "org_id": org_id,
            "triggered_by": triggered_by,
            stats_key: count,
        },
    )


def register_archive_consumers(adapter: ArchiveStorageAdapter) -> None:
    """Subscribe the SQL-only lifecycle handlers (archive + purge).
    Called by core-worker (SaaS) and core-api (OSS standalone).
    """

    async def archive_expired_op(req: LifecycleArchiveRequest) -> int:
        return await adapter.archive_expired(org_id=req.org_id, fleet_id=req.fleet_id)

    async def archive_stale_op(req: LifecycleArchiveRequest) -> int:
        return await adapter.archive_stale(org_id=req.org_id, fleet_id=req.fleet_id)

    async def purge_op(req: LifecyclePurgeRequest) -> int:
        return await adapter.purge_soft_deleted(
            org_id=req.org_id,
            fleet_id=req.fleet_id,
            retention_days=req.retention_days,
        )

    bus = get_event_bus()
    bus.subscribe(
        Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED,
        partial(
            _run_action,
            adapter=adapter,
            payload_cls=LifecycleArchiveRequest,
            run_op=archive_expired_op,
            stats_key="archived",
            action="archive-expired",
        ),
    )
    bus.subscribe(
        Topics.Lifecycle.ARCHIVE_STALE_REQUESTED,
        partial(
            _run_action,
            adapter=adapter,
            payload_cls=LifecycleArchiveRequest,
            run_op=archive_stale_op,
            stats_key="archived",
            action="archive-stale",
        ),
    )
    bus.subscribe(
        Topics.Lifecycle.PURGE_SOFT_DELETED_REQUESTED,
        partial(
            _run_action,
            adapter=adapter,
            payload_cls=LifecyclePurgeRequest,
            run_op=purge_op,
            stats_key="deleted",
            action="purge-soft-deleted",
        ),
    )


def register_pipeline_consumers(adapter: PipelineStorageAdapter) -> None:
    """Subscribe the LLM-heavy lifecycle handlers (crystallize +
    entity-link). Called ONLY by core-api — these consumers need
    core-api's pipeline machinery and can't run in core-worker today.

    Both ops use the dedup gate: a successful run within the last 23
    hours short-circuits a re-trigger to a no-op success record. The
    daily cron interval gives a 1-hour slack window before the next
    legitimate tick clears the dedup.
    """

    async def crystallize_op(req: LifecycleArchiveRequest) -> int:
        return await adapter.crystallize(org_id=req.org_id, fleet_id=req.fleet_id)

    async def entity_link_op(req: LifecycleArchiveRequest) -> int:
        return await adapter.entity_link(org_id=req.org_id, fleet_id=req.fleet_id)

    async def insights_op(req: LifecycleArchiveRequest) -> int:
        return await adapter.insights(org_id=req.org_id, fleet_id=req.fleet_id)

    bus = get_event_bus()
    bus.subscribe(
        Topics.Lifecycle.CRYSTALLIZE_REQUESTED,
        partial(
            _run_action,
            adapter=adapter,
            payload_cls=LifecycleArchiveRequest,
            run_op=crystallize_op,
            stats_key="links_or_clusters",
            action="crystallize",
            dedup_window_hours=_PIPELINE_DEDUP_WINDOW_HOURS,
        ),
    )
    bus.subscribe(
        Topics.Lifecycle.ENTITY_LINK_REQUESTED,
        partial(
            _run_action,
            adapter=adapter,
            payload_cls=LifecycleArchiveRequest,
            run_op=entity_link_op,
            stats_key="links_created",
            action="entity-link",
            dedup_window_hours=_PIPELINE_DEDUP_WINDOW_HOURS,
        ),
    )
    bus.subscribe(
        Topics.Lifecycle.INSIGHTS_REQUESTED,
        partial(
            _run_action,
            adapter=adapter,
            payload_cls=LifecycleArchiveRequest,
            run_op=insights_op,
            stats_key="insights_created",
            action="insights",
            dedup_window_hours=_PIPELINE_DEDUP_WINDOW_HOURS,
        ),
    )


# Back-compat: pre-CAURA-657, ``register_consumers(adapter)`` registered
# all three SQL ops. Existing call sites in core-api (OSS standalone)
# and core-worker still call this. Forwards to the archive registration
# so the existing wiring keeps working unchanged; pipeline ops register
# via :func:`register_pipeline_consumers` from a separate site.
register_consumers = register_archive_consumers

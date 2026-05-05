"""Consumers for ``memclaw.lifecycle.<action>-requested`` topics
(CAURA-655 archive ops, CAURA-656 purge-soft-deleted op).

Lives in ``common/`` rather than under either service so the same code
runs in both deployments:

* SaaS — core-worker subscribes (``EVENT_BUS_BACKEND=pubsub``).
* OSS standalone — core-api subscribes against the in-process bus
  (no separate worker process to consume the in-memory queue).

The handler delegates the two storage round-trips it needs (run the
SQL primitive, finalise the audit row) to a small adapter the host
service supplies via :func:`register_consumers`. Per-action ops bind
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


class LifecycleStorageAdapter(Protocol):
    """Three-method shape the lifecycle handlers need.

    All methods are async and call core-storage-api over HTTP. Hosts
    inject their own implementation so this module stays free of any
    core-api / core-worker imports.
    """

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
    adapter: LifecycleStorageAdapter,
    payload_cls: type[LifecycleRequestBase],
    run_op: _OpFn,
    stats_key: str,
    action: str,
) -> None:
    """Shared body for every lifecycle action — bound to a specific
    primitive at registration time so this function never branches on
    a string. SQL ops are naturally idempotent so Pub/Sub redelivery
    is safe; each delivery attempt updates the SAME audit row (the
    row id rides in the payload, pre-created by the fanout endpoint).
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

    # Best-effort in_progress mark: 404 means the audit row was pruned
    # between fanout and consume. Continue anyway so the SQL primitive
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
        # from a crashed worker. Log and continue so the original
        # always surfaces.
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


def register_consumers(adapter: LifecycleStorageAdapter) -> None:
    """Subscribe every lifecycle action handler. Per-action ops are
    bound here as closures over the adapter so :func:`_run_action`
    receives a no-arg-ish callable + payload class. Call once at app
    startup, before ``bus.start()``.
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

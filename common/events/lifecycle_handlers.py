"""Consumers for ``memclaw.lifecycle.<action>-requested`` topics (CAURA-655).

Lives in ``common/`` rather than under either service so the same code
runs in both deployments:

* SaaS — core-worker subscribes (``EVENT_BUS_BACKEND=pubsub``).
* OSS standalone — core-api subscribes against the in-process bus
  (no separate worker process to consume the in-memory queue).

The handler delegates the two storage round-trips it needs (run the
SQL primitive, finalise the audit row) to a small adapter the host
service supplies via :func:`register_consumers`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol

from pydantic import ValidationError

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.lifecycle_archive_request import LifecycleArchiveRequest
from common.events.topics import Topics

logger = logging.getLogger(__name__)


class LifecycleStorageAdapter(Protocol):
    """Two-method shape the lifecycle handlers need.

    All methods are async and call core-storage-api over HTTP. Hosts
    inject their own implementation so this module stays free of any
    core-api / core-worker imports.
    """

    async def archive_expired(self, *, org_id: str, fleet_id: str | None) -> int: ...

    async def archive_stale(self, *, org_id: str, fleet_id: str | None) -> int: ...

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None: ...


_ArchiveFn = Callable[..., Awaitable[int]]


async def _run_archive(
    event: Event,
    *,
    adapter: LifecycleStorageAdapter,
    archive_fn: _ArchiveFn,
    action: str,
) -> None:
    """Shared body for both archive handlers — bound to a specific
    primitive at registration time so this function never branches on
    a string. The SQL archive ops are naturally idempotent so Pub/Sub
    redelivery is safe to run more than once; each delivery attempt
    updates the SAME audit row (the row id rides in the event payload
    and is pre-created by the fanout endpoint before publish).
    """
    try:
        request = LifecycleArchiveRequest(**event.payload)
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

    # Best-effort in_progress mark: 404 means the audit row was pruned
    # between fanout and consume. Continue anyway so the SQL primitive
    # still runs — dropping would silently skip an op the operator
    # asked for.
    try:
        await adapter.update_lifecycle_audit_row(request.audit_id, status="in_progress")
    except Exception:
        logger.warning(
            "lifecycle audit in_progress update failed; continuing",
            exc_info=True,
            extra={"audit_id": request.audit_id, "action": action},
        )

    try:
        count = await archive_fn(org_id=request.org_id, fleet_id=request.fleet_id)
    except Exception as exc:
        # Mark the row failed so observers can distinguish a stuck
        # ``in_progress`` (worker crashed mid-run) from a finished-with-
        # error op. Truncated to keep the row bounded; full traceback
        # is in the worker logs.
        #
        # Wrap the audit update in its own guard: if it raises, the
        # outer ``raise`` below would never run and the original archive
        # exception would be silently replaced by the audit error,
        # leaving the row stuck in ``in_progress`` indistinguishable
        # from a crashed worker. Log and continue so the original
        # always surfaces.
        try:
            await adapter.update_lifecycle_audit_row(
                request.audit_id,
                status="failure",
                error_message=str(exc)[:500],
            )
        except Exception:
            logger.warning(
                "lifecycle audit failure update failed; row stuck in_progress",
                exc_info=True,
                extra={"audit_id": request.audit_id, "action": action},
            )
        # Re-raise so the bus nacks → Pub/Sub redelivers (subject to
        # max-delivery-attempts → DLQ). The ``failure`` row above is
        # the durable record (when the update succeeded).
        raise

    await adapter.update_lifecycle_audit_row(
        request.audit_id,
        status="success",
        stats={"archived": count},
    )

    logger.info(
        "lifecycle %s processed",
        action,
        extra={
            "audit_id": request.audit_id,
            "org_id": request.org_id,
            "triggered_by": request.triggered_by,
            "archived": count,
        },
    )


def register_consumers(adapter: LifecycleStorageAdapter) -> None:
    """Subscribe both archive handlers, each closing over the adapter
    method that runs its primitive. Call once at app startup, before
    ``bus.start()``.
    """
    bus = get_event_bus()
    bus.subscribe(
        Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED,
        partial(
            _run_archive,
            adapter=adapter,
            archive_fn=adapter.archive_expired,
            action="archive-expired",
        ),
    )
    bus.subscribe(
        Topics.Lifecycle.ARCHIVE_STALE_REQUESTED,
        partial(
            _run_archive,
            adapter=adapter,
            archive_fn=adapter.archive_stale,
            action="archive-stale",
        ),
    )

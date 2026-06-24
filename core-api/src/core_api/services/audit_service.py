"""Audit-event ingestion entrypoint.

``log_action`` is the only public surface — every memory mutation calls
it via the configured ``ServiceHooks`` (see ``core_api.app.lifespan``).

CAURA-628 (2026-04-29): the legacy shape POSTed one audit event per
mutation directly to ``core-storage-api``. Under bulk storms that
created up to N HTTP-POSTs-per-bulk-create on the storage-api connection
pool + serialised tenant B's audit traffic behind tenant A's storm at
the AlloyDB ``audit_log`` table-level write lock. ``log_action`` now
enqueues into a process-local ``AuditEventQueue``; a background flusher
batches events and writes them via ``POST /audit-logs/bulk``.

The synchronous-POST fallback path runs when the queue is not active
(early startup, tests that didn't wire it, intentional kill-switch via
``set_audit_queue(None)``). That fallback preserves the legacy
behaviour byte-for-byte so a queue-side bug can't silently drop audit
events; an operator can fall back to the legacy path during an incident
without a redeploy.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from core_api.clients.storage_client import get_storage_client
from core_api.services.audit_queue import get_audit_queue

logger = logging.getLogger(__name__)


async def _post_audit_sync(payload: dict) -> None:
    """Synchronous fallback write to core-storage-api (same shape as
    pre-CAURA-628). Used both when the async queue is inactive and as the
    ``critical`` overflow fallback, so a compliance event isn't dropped."""
    sc = get_storage_client()
    await sc.create_audit_log(payload)


async def log_action(
    *,
    tenant_id: str,
    agent_id: str | None = None,
    action: str,
    resource_type: str,
    resource_id: UUID | str | None = None,
    detail: dict | None = None,
    critical: bool = False,
) -> None:
    """Record an audit event. Async-batched via queue when available;
    falls through to a synchronous POST otherwise.

    ``db`` is unused (audit persistence is owned by the storage layer)
    but kept in the signature for back-compat with the ``ServiceHooks``
    contract — callers pass it through; switching them all to drop the
    arg is a separate, scoped change. ``None`` is accepted for
    fire-and-forget callers with no ambient session (the post-enrichment
    governance remediation in the ENRICHED consumer).

    ``critical`` marks a compliance-critical event (e.g. a governance
    enforcement *rejection*) that must not be silently dropped under
    queue overflow: when the queue is full, it falls back to a
    synchronous storage POST instead of dropping. Non-critical events
    keep the fire-and-forget enqueue (drop-with-counter on overflow), so
    the high-volume hot path is unaffected.
    """
    payload = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": str(resource_id) if resource_id else None,
        "detail": detail,
        # Per-event dedup key: lets the async bulk flush retry a lost-ack POST
        # without double-appending to the tamper-evident chain (storage dedups
        # on it under the per-tenant head lock). Minted here so it's stable
        # across a retry of the same enqueued event.
        "client_event_id": str(uuid4()),
    }
    queue = get_audit_queue()
    if queue is not None:
        # Non-blocking enqueue. Overflow is mapped to a structured
        # warning + drop counter inside the queue — the request hot
        # path stays fast even when storage-api is degraded. For a
        # ``critical`` event pass ``silent=True``: we recover it via the
        # sync write below on overflow, so the queue must NOT count it as a
        # drop or log it as lost (it isn't).
        enqueued = queue.enqueue(payload, silent=critical)
        if enqueued or not critical:
            return
        # Critical event rejected by a full queue: sync POST so the decision
        # isn't dropped. If that ALSO fails (queue saturated AND storage down),
        # log loudly but do NOT propagate — a critical audit precedes an
        # enforcement action (a 4xx reject or a soft-delete), and a failing
        # audit must not block it (else the row the policy means to drop stays).
        logger.warning("audit queue full; writing critical %r event synchronously", action)
        try:
            await _post_audit_sync(payload)
        except Exception:
            logger.exception(
                "audit queue full AND sync fallback failed; critical %r event LOST "
                "(storage degraded?) — proceeding so enforcement isn't blocked",
                action,
            )
        return

    # Queue inactive (early startup / tests / kill-switch): synchronous POST.
    # This is the PRIMARY write for ALL events in this mode (not just critical),
    # so it must RAISE on failure — do NOT swallow like the critical-overflow
    # fallback above, or an operator's deliberate fallback-to-legacy would
    # become a silent audit blackout.
    await _post_audit_sync(payload)


async def log_cross_tenant_read(
    *,
    home_tenant_id: str | None,
    home_agent_id: str | None,
    source_tenants: list[str],
    surface: str,
    result_count_by_tenant: dict[str, int] | None = None,
    query_summary: str | None = None,
) -> None:
    """Emit a ``cross_tenant_read`` audit event per source tenant touched.

    Called from read handlers after they widen their query via
    ``readable_tenant_ids``. The event is logged TO each source tenant
    (``tenant_id=src``) so per-tenant audit-log queries surface "who
    read FROM my tenant" — including the home tenant_id and agent_id
    of the caller in ``detail`` for forensic traceability.

    No-op when ``source_tenants`` is empty (single-tenant reads).
    Emission is via the same async queue ``log_action`` uses — overflow
    handling and back-pressure are identical.

    Hook for ``AuthContext.source_tenants_for_audit()`` — callers pass
    that method's return value as ``source_tenants``.
    """
    if not source_tenants:
        return
    for src in source_tenants:
        await log_action(
            tenant_id=src,
            agent_id=home_agent_id,
            action="cross_tenant_read",
            resource_type=surface,
            detail={
                "home_tenant_id": home_tenant_id,
                "home_agent_id": home_agent_id,
                "result_count_from_this_tenant": ((result_count_by_tenant or {}).get(src)),
                "query_summary": query_summary,
            },
        )

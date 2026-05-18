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

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core_api.clients.storage_client import get_storage_client
from core_api.services.audit_queue import get_audit_queue


async def log_action(
    db: AsyncSession,
    *,
    tenant_id: str,
    agent_id: str | None = None,
    action: str,
    resource_type: str,
    resource_id: UUID | None = None,
    detail: dict | None = None,
) -> None:
    """Record an audit event. Async-batched via queue when available;
    falls through to a synchronous POST otherwise.

    ``db`` is unused (audit persistence is owned by the storage layer)
    but kept in the signature for back-compat with the ``ServiceHooks``
    contract — callers pass it through; switching them all to drop the
    arg is a separate, scoped change.
    """
    payload = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": str(resource_id) if resource_id else None,
        "detail": detail,
    }
    queue = get_audit_queue()
    if queue is not None:
        # Non-blocking enqueue. Overflow is mapped to a structured
        # warning + drop counter inside the queue — the request hot
        # path stays fast even when storage-api is degraded.
        queue.enqueue(payload)
        return

    # Fallback: synchronous POST. Same shape as pre-CAURA-628.
    sc = get_storage_client()
    await sc.create_audit_log(payload)


async def log_cross_tenant_read(
    db: AsyncSession,
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
            db,
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

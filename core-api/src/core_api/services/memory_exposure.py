"""Memory exposure: record which agents a read handed each memory to (migration 072).

Correction propagation (``docs/plans/correction-propagation.md``), D1. Every
read that returns memories to an agent calls ``schedule_exposure`` once: MCP
``caura_recall`` and REST ``/search`` / ``/recall`` through the search
pipeline's ``RecordExposure`` step, and get-by-id (REST ``GET /memories/{id}``,
MCP ``caura_manage op=read``) directly.

The rules, all here so the call sites stay one line:

* A chunk or an atomic fact counts for its parent (``parent_memory_id``): the
  parent is what a person corrects, and the agent read part of it.
* A read with no agent identity, or by a signed-in person, records nothing.
* One storage call per tenant the rows came from (one, unless a cross-tenant
  credential widened the read), fire-and-forget after the response. A failure
  is logged, never raised: recording must not slow or break a read.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from core_api.audit_actor import audit_principal
from core_api.clients.storage_client import get_storage_client
from core_api.session_context import server_trace_id
from core_api.tasks import track_task

logger = logging.getLogger(__name__)

#: Headroom over ``MAX_SEARCH_TOP_K``; the storage route refuses more.
_MAX_IDS = 500


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def exposure_targets(rows: Iterable[Any], *, default_tenant: str | None) -> dict[str, list[str]]:
    """The memory ids a read exposed, by tenant: each row's own id, or its parent's."""
    from core_api.pipeline.steps.search.record_recall_value import parent_of

    targets: dict[str, dict[str, None]] = {}
    for row in rows:
        # STM-injected context is not a stored memory.
        if _field(row, "injected"):
            continue
        memory_id = _field(row, "id")
        if not memory_id:
            continue
        # Search hits carry ``metadata``; storage's by-id dict ``metadata_``.
        metadata = _field(row, "metadata")
        if metadata is None:
            metadata = _field(row, "metadata_")
        target = parent_of(metadata) or str(memory_id)
        tenant = _field(row, "tenant_id") or default_tenant
        if tenant:
            targets.setdefault(str(tenant), {})[target] = None
    return {tenant: list(ids)[:_MAX_IDS] for tenant, ids in targets.items()}


def _is_person() -> bool:
    principal = audit_principal.get() or {}
    return principal.get("credential_kind") == "person"


async def _record(tenant_id: str, body: dict) -> None:
    try:
        await get_storage_client().record_exposure(tenant_id, body)
    except Exception:
        logger.warning("memory exposure not recorded", exc_info=True)


def schedule_exposure(rows: Iterable[Any] | None, *, agent_id: str | None, tenant_id: str | None) -> None:
    """Record that ``agent_id`` was handed ``rows``, after the response."""
    try:
        if not rows or not agent_id or _is_person():
            return
        by_tenant = exposure_targets(rows, default_tenant=tenant_id)
        if not by_tenant:
            return
        # Captured here, inside the request: the background task outlives it.
        seen_at = datetime.now(UTC).isoformat()
        trace_id = server_trace_id()
        for tenant, ids in by_tenant.items():
            track_task(
                _record(
                    tenant,
                    {"agent_id": str(agent_id), "memory_ids": ids, "seen_at": seen_at, "trace_id": trace_id},
                )
            )
    except Exception:
        logger.warning("memory exposure skipped due to error", exc_info=True)

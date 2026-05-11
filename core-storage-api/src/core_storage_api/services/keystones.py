"""Keystone-rules service.

Keystones are governance rules an agent must obey. They are stored as
documents in the system-managed collection ``_keystones`` so we get
upsert-by-doc_id, JSONB payload, and tenant/fleet isolation for free —
no schema migration, no new table.

Resolution returns the union of three scopes:

* ``tenant`` — fleet_id IS NULL, applies org-wide.
* ``fleet``  — fleet_id matches, applies to every agent in the fleet.
* ``agent``  — fleet_id matches AND data.agent_id matches.

Ordered by ``data.weight DESC, updated_at DESC`` and capped at
``KEYSTONE_MAX_RESULTS`` so a runaway tenant can't bloat the
plugin/MCP context window.
"""

from __future__ import annotations

from sqlalchemy import or_, select

from common.models import Document
from core_storage_api.services.postgres_service import get_read_session

# ---------------------------------------------------------------------------
# Constants — kept local (no global constants module exists in core-storage-api).
# ---------------------------------------------------------------------------

KEYSTONE_COLLECTION = "_keystones"

KEYSTONE_MAX_RESULTS = 50

# Fixed weight buckets — admins choose a label, we map to an int.
# Buckets (not free-form) keep ranking predictable across authors.
KEYSTONE_WEIGHT_BUCKETS: dict[str, int] = {
    "low": 25,
    "med": 50,
    "high": 100,
}

KEYSTONE_VALID_SCOPES: frozenset[str] = frozenset({"tenant", "fleet", "agent"})


async def list_keystones(
    *,
    tenant_id: str,
    fleet_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[list[Document], bool]:
    """Return ``(docs, truncated)`` for ``(tenant_id, fleet_id, agent_id)``.

    The result is the scope union — see module docstring. Empty fleet/agent
    args narrow the result, never broaden it (e.g. fleet=None drops fleet
    AND agent rules).

    ``truncated`` is True when more than ``KEYSTONE_MAX_RESULTS`` rules
    matched. We over-fetch by one and slice so callers can signal the
    cap was hit (e.g. via an ``X-Truncated`` response header) — silent
    truncation hides governance gaps.
    """
    # Build OR predicates per scope. We always include tenant scope; fleet
    # and agent layers stack on top when their inputs are present.
    predicates = [
        # tenant: fleet_id IS NULL, data.scope == 'tenant'
        (Document.fleet_id.is_(None)) & (Document.data["scope"].astext == "tenant"),
    ]

    if fleet_id is not None:
        predicates.append((Document.fleet_id == fleet_id) & (Document.data["scope"].astext == "fleet"))

    if fleet_id is not None and agent_id is not None:
        predicates.append(
            (Document.fleet_id == fleet_id)
            & (Document.data["scope"].astext == "agent")
            & (Document.data["agent_id"].astext == agent_id)
        )

    stmt = (
        select(Document)
        .where(
            Document.tenant_id == tenant_id,
            Document.collection == KEYSTONE_COLLECTION,
            or_(*predicates),
        )
        # Order by JSON weight DESC; fall back to updated_at to stabilise
        # ties so ranking is deterministic across calls.
        #
        # NOTE: data["weight"] sort requires a functional index:
        #   CREATE INDEX idx_keystones_weight ON documents
        #   (tenant_id, ((data->>'weight')::int) DESC, updated_at DESC)
        #   WHERE collection = '_keystones';
        # Without it this is a sequential scan. Track in #112.
        .order_by(
            Document.data["weight"].as_float().desc(),
            Document.updated_at.desc(),
        )
        # Over-fetch by one so the router can detect truncation.
        .limit(KEYSTONE_MAX_RESULTS + 1)
    )

    async with get_read_session() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
    truncated = len(rows) > KEYSTONE_MAX_RESULTS
    return rows[:KEYSTONE_MAX_RESULTS], truncated

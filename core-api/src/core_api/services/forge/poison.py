"""Poison-memory writer + checker for Forge cluster fingerprints
(SF-208).

When a candidate is REJECTED in the Inbox (Phase 2) or auto-gates
classify a fingerprint as un-promotable for a reason the user surfaced
explicitly, we write a row to ``forge_rejected_fingerprints`` so the
next Forge run skips that cluster for ``cooloff_days``.

Plan §15 Phase 2 acceptance gate:

  "Reject → fingerprint written to poison table; Forge re-run does NOT
   propose the same fingerprint."

This module wraps that write + the symmetric lookup query that the
auto-gate evaluator's ``poison_checker`` callable hits. The same query
shape is used by the Forge CLI status-checker (SF-106) — keep the
two predicates in lockstep with the index defined in
``020_forge_rejected_fingerprints.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)


# Default cooloff. Mirrored in ``org_settings.skills_factory.rejection_cooloff_days``;
# the route that calls ``write_rejected_fingerprint`` resolves the
# tenant override and passes it in.
DEFAULT_COOLOFF_DAYS: int = 30


async def write_rejected_fingerprint(
    db: Any,
    *,
    tenant_id: str,
    fleet_id: str | None,
    cluster_fingerprint: str,
    rejected_by_agent: str,
    reason: str | None = None,
    cooloff_days: int = DEFAULT_COOLOFF_DAYS,
) -> str:
    """Insert one ``forge_rejected_fingerprints`` row.

    Returns the new row's ``id`` (UUID). Idempotency:
    multiple rejections of the same fingerprint stack — the
    poison-check predicate ``rejected_at + cooloff_days > now()`` is
    satisfied by *any* live row, so re-rejecting just extends the
    cooloff (newest row's expiry wins). We do NOT dedup here — an
    operator may reject the same cluster twice with different reasons,
    and the audit trail benefits from both rows.
    """
    if not cluster_fingerprint:
        raise ValueError("cluster_fingerprint must be non-empty")
    if cooloff_days < 1:
        raise ValueError(f"cooloff_days must be >= 1 (got {cooloff_days})")

    row = (
        await db.execute(
            text(
                """
                INSERT INTO forge_rejected_fingerprints
                    (tenant_id, fleet_id, cluster_fingerprint,
                     rejected_by_agent, cooloff_days, reason)
                VALUES
                    (:tenant_id, :fleet_id, :cluster_fingerprint,
                     :rejected_by_agent, :cooloff_days, :reason)
                RETURNING id::text AS id
                """
            ),
            {
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "cluster_fingerprint": cluster_fingerprint,
                "rejected_by_agent": rejected_by_agent,
                "cooloff_days": cooloff_days,
                "reason": reason,
            },
        )
    ).fetchone()
    new_id = row.id if row else "unknown"
    logger.info(
        "poisoned cluster fingerprint",
        extra={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "cluster_fingerprint": cluster_fingerprint,
            "rejected_by_agent": rejected_by_agent,
            "cooloff_days": cooloff_days,
            "row_id": new_id,
        },
    )
    return new_id


async def is_fingerprint_poisoned(
    db: Any,
    *,
    tenant_id: str,
    fleet_id: str | None,
    cluster_fingerprint: str,
) -> bool:
    """Return True iff ``forge_rejected_fingerprints`` has a row whose
    cooloff window is still live for this (tenant, fleet, fp) triple.

    Predicate mirrors the index defined in migration 020:

        WHERE tenant_id = :t
          AND cluster_fingerprint = :fp
          AND (fleet_id = :f OR fleet_id IS NULL)
          AND rejected_at + (interval '1 day' * cooloff_days) > now()

    Three-arm fleet predicate:

      * ``fleet_id IS NULL``        — tenant-wide rejections ALWAYS apply,
                                       regardless of which fleet is scanning.
      * ``:fleet_id IS NULL``       — when the SCAN itself is tenant-wide
                                       (the worker isn't restricted to a fleet),
                                       any fleet-scoped rejection for this fp
                                       blocks the candidate. Without this arm,
                                       a fleet-A rejection would be invisible
                                       to a tenant-wide Forge run and the
                                       cluster would be re-proposed.
      * ``fleet_id = :fleet_id``    — fleet-specific rejection applies to its
                                       own fleet's scans.

    Strictest-wins semantics: any matching row blocks the fingerprint.
    """
    row = (
        await db.execute(
            text(
                """
                SELECT 1
                FROM forge_rejected_fingerprints
                WHERE tenant_id = :tenant_id
                  AND cluster_fingerprint = :cluster_fingerprint
                  -- Three-arm fleet predicate (see docstring above):
                  --   1. tenant-wide rejection rows ALWAYS apply
                  --   2. when the SCAN is tenant-wide (:fleet_id IS NULL),
                  --      any fleet-scoped rejection blocks too — without
                  --      this arm a fleet-A rejection was invisible to
                  --      tenant-wide Forge runs and the cluster would
                  --      be re-proposed across the whole tenant.
                  --   3. matching fleet-id rows apply within their own fleet
                  AND (
                      fleet_id IS NULL
                      OR :fleet_id IS NULL
                      OR fleet_id = :fleet_id
                  )
                  -- ``interval '1 day' * cooloff_days`` is the
                  -- direct integer-times-interval form. The previous
                  -- ``(cooloff_days || ' days')::interval`` shape
                  -- relied on a Postgres integer||text concat that
                  -- does not exist, so the predicate raised
                  -- "operator does not exist: integer || unknown"
                  -- at runtime — the G4 poison gate ALWAYS failed
                  -- closed and auto-promotion never landed.
                  AND rejected_at + (interval '1 day' * cooloff_days) > now()
                LIMIT 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "cluster_fingerprint": cluster_fingerprint,
            },
        )
    ).fetchone()
    return row is not None

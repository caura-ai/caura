"""Poison-memory writer + checker for Forge cluster fingerprints
(SF-208).

When a candidate is REJECTED in the Inbox (Phase 2) or auto-gates
classify a fingerprint as un-promotable for a reason the user surfaced
explicitly, we write a row to ``forge_rejected_fingerprints`` so the
next Forge run skips that cluster for ``cooloff_days``.

Plan §15 Phase 2 acceptance gate:

  "Reject → fingerprint written to poison table; Forge re-run does NOT
   propose the same fingerprint."

As of Fix 2 Ph5a the write + the symmetric cooloff-window lookup go
through core-storage-api over HTTP (``sc.forge_write_rejected_fingerprint``
/ ``sc.forge_is_fingerprint_poisoned``); the PG-specific SQL (the 3-arm
fleet predicate + ``rejected_at + (interval '1 day' * cooloff_days) >
now()``) lives in ``PostgresService`` and is kept in lockstep with the
index defined in ``020_forge_rejected_fingerprints.py``.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)


# Default cooloff. Mirrored in ``org_settings.skills_factory.rejection_cooloff_days``;
# the route that calls ``write_rejected_fingerprint`` resolves the
# tenant override and passes it in.
DEFAULT_COOLOFF_DAYS: int = 30


async def write_rejected_fingerprint(
    *,
    tenant_id: str,
    fleet_id: str | None,
    cluster_fingerprint: str,
    rejected_by_agent: str,
    reason: str | None = None,
    cooloff_days: int = DEFAULT_COOLOFF_DAYS,
) -> str:
    """Insert one ``forge_rejected_fingerprints`` row via core-storage-api.

    Returns the new row's ``id`` (UUID). Idempotency:
    multiple rejections of the same fingerprint stack — the
    poison-check predicate ``rejected_at + cooloff_days > now()`` is
    satisfied by *any* live row, so re-rejecting just extends the
    cooloff (newest row's expiry wins). We do NOT dedup here — an
    operator may reject the same cluster twice with different reasons,
    and the audit trail benefits from both rows.

    The ``ValueError`` guards stay here (not storage-side) so the route's
    422 contract on ``cooloff_days < 1`` / empty fingerprint is unchanged.
    """
    if not cluster_fingerprint:
        raise ValueError("cluster_fingerprint must be non-empty")
    if cooloff_days < 1:
        raise ValueError(f"cooloff_days must be >= 1 (got {cooloff_days})")

    sc = get_storage_client()
    new_id = await sc.forge_write_rejected_fingerprint(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        cluster_fingerprint=cluster_fingerprint,
        rejected_by_agent=rejected_by_agent,
        cooloff_days=cooloff_days,
        reason=reason,
    )
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
    *,
    tenant_id: str,
    fleet_id: str | None,
    cluster_fingerprint: str,
) -> bool:
    """Return True iff ``forge_rejected_fingerprints`` has a row whose
    cooloff window is still live for this (tenant, fleet, fp) triple.

    The predicate (mirrored in migration 020 + ``PostgresService.
    forge_is_fingerprint_poisoned``):

        WHERE tenant_id = :t
          AND cluster_fingerprint = :fp
          AND (fleet_id = :f OR fleet_id IS NULL OR :f IS NULL)
          AND rejected_at + (interval '1 day' * cooloff_days) > now()

    Three-arm fleet predicate:

      * ``fleet_id IS NULL``        — tenant-wide rejections ALWAYS apply,
                                       regardless of which fleet is scanning.
      * ``:fleet_id IS NULL``       — when the SCAN itself is tenant-wide
                                       (the worker isn't restricted to a fleet),
                                       any fleet-scoped rejection for this fp
                                       blocks the candidate.
      * ``fleet_id = :fleet_id``    — fleet-specific rejection applies to its
                                       own fleet's scans.

    Strictest-wins semantics: any matching row blocks the fingerprint.
    """
    sc = get_storage_client()
    return await sc.forge_is_fingerprint_poisoned(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        cluster_fingerprint=cluster_fingerprint,
    )

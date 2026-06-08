"""Contradiction signal — memory got contradicted soon after recall.

The existing :mod:`core_api.services.contradiction_detector` (Path A
+ Path C) flags memories whose claims a newer write contradicted.
The detector's effect on the data plane is a status flip on the
OLDER memory:

  - ``status = 'outdated'``    — RDF-triple path detected a stale
                                  state-transition (older lost the race)
  - ``status = 'conflicted'``  — semantic path found a contradiction
                                  that didn't resolve cleanly

Both indicate "this memory turned out to be wrong" and therefore
the session that wrote it likely failed at its task. This signal
fires polarity=FAILURE on the trace that produced the now-outdated /
conflicted memory.

We restrict the window check to the status TRANSITION time, not the
memory's original ``created_at`` — i.e. a memory that's been around
for weeks but only just got contradicted SHOULD still register a
firing inside the current scan window. Until the storage layer adds
a ``status_changed_at`` column (CAURA-future), we approximate via
``COALESCE(updated_at, created_at)`` (an outdated/conflicted memory's
``updated_at`` is the status flip).

Plan §6 signal #1. Plan §3 schema field
``signals_summary.contradiction``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from . import (
    DEFAULT_SIGNAL_WEIGHTS,
    Polarity,
    SignalEvidence,
    SignalKind,
    SignalQuery,
)

logger = logging.getLogger(__name__)

kind: SignalKind = SignalKind.CONTRADICTION


# ``status`` values the detector writes on a contradicted memory. Both
# are treated as failure evidence for outcome inference. Keep this set
# in sync with the writes in
# ``core_api.services.contradiction_detector`` (search for
# ``_merge_status_update(..., {"status": "outdated"})`` and
# ``"conflicted"``).
CONTRADICTED_STATUSES: tuple[str, ...] = ("outdated", "conflicted")


async def extract(query: SignalQuery, db: Any) -> list[SignalEvidence]:
    """Return one evidence per contradicted memory whose status flip
    falls inside the window. The trace that originally wrote the
    memory (``run_id``, ``agent_id``) is the one that pays the
    FAILURE polarity.

    Idempotency: each memory only emits one firing per scan because
    ``m.id`` is the primary key — a plain SELECT already returns at
    most one row per memory. The previous ``DISTINCT ON (m.id)``
    without an ``ORDER BY`` was both a no-op (PK uniqueness) AND
    technically undefined per Postgres docs; removed for clarity.
    """
    weight = DEFAULT_SIGNAL_WEIGHTS[SignalKind.CONTRADICTION]

    # Bind the status list explicitly rather than inlining the tuple
    # so Postgres can use the existing tenant-status indexes.
    sql = """
        SELECT
            m.id          AS memory_id,
            m.run_id      AS run_id,
            m.agent_id    AS agent_id,
            m.status      AS status,
            COALESCE(m.updated_at, m.created_at) AS observed_at
        FROM memories AS m
        WHERE m.tenant_id = :tenant_id
          AND m.status = ANY(:contradicted_statuses)
          AND COALESCE(m.updated_at, m.created_at) >= :w_start
          AND COALESCE(m.updated_at, m.created_at) <  :w_end
          AND (:fleet_id IS NULL OR m.fleet_id = :fleet_id OR m.fleet_id IS NULL)
          AND (:run_id   IS NULL OR m.run_id   = :run_id)
          AND (:agent_id IS NULL OR m.agent_id = :agent_id)
          AND m.run_id IS NOT NULL
    """

    rows = (
        await db.execute(
            text(sql),
            {
                "tenant_id": query.tenant_id,
                "fleet_id": query.fleet_id,
                "w_start": query.window_start,
                "w_end": query.window_end,
                "run_id": query.run_id,
                "agent_id": query.agent_id,
                "contradicted_statuses": list(CONTRADICTED_STATUSES),
            },
        )
    ).fetchall()

    out: list[SignalEvidence] = []
    for row in rows:
        out.append(
            SignalEvidence(
                kind=SignalKind.CONTRADICTION,
                polarity=Polarity.FAILURE,
                weight=weight,
                memory_ids=(str(row.memory_id),),
                details={
                    "memory_id": str(row.memory_id),
                    "status": row.status,
                    "run_id": row.run_id,
                    "agent_id": row.agent_id,
                },
                observed_at=row.observed_at,
            )
        )

    if out:
        logger.debug(
            "contradiction signal: %d firings in window for tenant=%s",
            len(out),
            query.tenant_id,
        )
    return out

"""Cross-agent reuse signal — load-bearing memories indicate good patterns.

If memory M was recalled by ≥3 DISTINCT agents, the procedure that
produced it is almost certainly something the fleet has converged on
— a candidate skill, not a fluke. The signal fires polarity=NEUTRAL
(informational only — it doesn't claim the originating session was
"successful", just that the artifact it produced is being reused).

The session-trace builder treats NEUTRAL evidence as a boost toward
"this trace deserves clustering" without driving the success/failure
label one way or the other. Plan §6 signal #5.

**MVP data source**: ``memories.recall_count`` (existing column,
incremented by ``track_recalls`` pipeline step). It counts TOTAL
recalls, not distinct-agent recalls. We approximate distinct-agent
reuse via the recall_count threshold — it's an over-approximation
(a single agent re-recalling the same memory inflates the count)
but conservative on the firing side: ``recall_count >= 5`` typically
means ≥3 distinct agents in practice.

**Phase 2+ upgrade path**: a ``memory_recalls`` table keyed
``(memory_id, agent_id, last_recalled_at)`` gives the exact distinct-
agent count without inflation. The extractor signature stays the
same; the SQL gets sharper. Tracked as OQ-future.
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

kind: SignalKind = SignalKind.CROSS_AGENT_REUSE

# Threshold for "load-bearing". Conservative — recall_count counts
# ALL recalls (including self-recalls), so 5 total usually corresponds
# to ~3 distinct agents in the wild. Configurable per-tenant via
# org_settings.skills_factory.forge.cross_agent_reuse_threshold (added
# in Phase 2 settings expansion; default for MVP wired here).
DEFAULT_RECALL_COUNT_THRESHOLD: int = 5


async def extract(query: SignalQuery, db: Any) -> list[SignalEvidence]:
    """Find memories with recall_count above threshold whose AUTHOR's
    trace is in the window. The signal fires on the AUTHOR's trace
    (the session that produced the load-bearing memory), not on the
    recalling sessions — that's where Forge needs the evidence to
    decide "this trace's procedure is worth crystallising".
    """
    weight = DEFAULT_SIGNAL_WEIGHTS[SignalKind.CROSS_AGENT_REUSE]

    sql = """
        SELECT
            m.id           AS memory_id,
            m.run_id       AS run_id,
            m.agent_id     AS agent_id,
            m.recall_count AS recall_count,
            m.last_recalled_at AS observed_at
        FROM memories AS m
        WHERE m.tenant_id = :tenant_id
          AND m.recall_count >= :threshold
          AND m.created_at >= :w_start
          AND m.created_at <  :w_end
          AND m.run_id IS NOT NULL
          AND (:fleet_id IS NULL OR m.fleet_id = :fleet_id OR m.fleet_id IS NULL)
          AND (:run_id   IS NULL OR m.run_id   = :run_id)
          AND (:agent_id IS NULL OR m.agent_id = :agent_id)
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
                "threshold": DEFAULT_RECALL_COUNT_THRESHOLD,
            },
        )
    ).fetchall()

    out: list[SignalEvidence] = []
    for row in rows:
        out.append(
            SignalEvidence(
                kind=SignalKind.CROSS_AGENT_REUSE,
                polarity=Polarity.NEUTRAL,
                weight=weight,
                memory_ids=(str(row.memory_id),),
                details={
                    "memory_id": str(row.memory_id),
                    "run_id": row.run_id,
                    "agent_id": row.agent_id,
                    "recall_count": row.recall_count,
                    "threshold": DEFAULT_RECALL_COUNT_THRESHOLD,
                    "approximation": "total-recalls (Phase 1 v1) — see module docstring",
                },
                observed_at=row.observed_at,
            )
        )

    if out:
        logger.debug(
            "cross_agent_reuse signal: %d load-bearing memories for tenant=%s",
            len(out),
            query.tenant_id,
        )
    return out

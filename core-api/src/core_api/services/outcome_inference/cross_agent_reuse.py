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

**Phase 2+ upgrade path**: ``recall_event`` + ``recall_candidate``
(migration 027) already shipped, and ``recall_event`` carries
``agent_id`` — so joining the two gives the exact distinct-agent count
without the self-recall inflation described above. The extractor
signature stays the same; the SQL gets sharper.

The ``memory_recalls`` table this file used to name as that path,
keyed ``(memory_id, agent_id, last_recalled_at)``, was never built and
is not what shipped — do not go looking for it, and do not carry it
forward as OQ-future work. ``repeat_recall`` names the same
non-existent table for the same reason and was corrected first.

**READ THIS BEFORE WRITING THE PHASE 2 QUERY — the obvious join is
cross-tenant.** ``recall_candidate`` has NO ``tenant_id`` of its own
and NO foreign key on ``memory_id``; it is scoped only by the ``ON
DELETE CASCADE`` from ``recall_event``, and ``POST
/memories/recall-log`` does not validate the candidate ids it is
handed — core-storage-api constructs each row verbatim. So a
``recall_candidate`` row CAN name a memory belonging to another
tenant.

That matters more for THIS signal than for ``repeat_recall``, because
this one counts DISTINCT ``agent_id`` per memory: an unconstrained
join would count another tenant's agents toward a memory's reuse
score, which both inflates the count and makes "some other tenant
recalled this too" observable in this tenant's signal. Constrain on
``recall_event.tenant_id`` explicitly; the cascade is not a tenant
predicate.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client

from . import (
    DEFAULT_SIGNAL_WEIGHTS,
    Polarity,
    SignalEvidence,
    SignalKind,
    SignalQuery,
    parse_observed_at,
)

logger = logging.getLogger(__name__)

kind: SignalKind = SignalKind.CROSS_AGENT_REUSE

# Threshold for "load-bearing". Conservative — recall_count counts
# ALL recalls (including self-recalls), so 5 total usually corresponds
# to ~3 distinct agents in the wild. Configurable per-tenant via
# org_settings.skills_factory.forge.cross_agent_reuse_threshold (added
# in Phase 2 settings expansion; default for MVP wired here).
DEFAULT_RECALL_COUNT_THRESHOLD: int = 5


async def extract(query: SignalQuery) -> list[SignalEvidence]:
    """Find memories with recall_count above threshold whose AUTHOR's
    trace is in the window. The signal fires on the AUTHOR's trace
    (the session that produced the load-bearing memory), not on the
    recalling sessions — that's where Forge needs the evidence to
    decide "this trace's procedure is worth crystallising".

    As of Fix 2 Ph5a the analytic read goes through core-storage-api
    (``sc.outcome_cross_agent_reuse_signals``); the ``recall_count >=
    :threshold`` + window SQL lives in
    ``PostgresService.outcome_cross_agent_reuse_signals``.
    """
    weight = DEFAULT_SIGNAL_WEIGHTS[SignalKind.CROSS_AGENT_REUSE]

    rows = await get_storage_client().outcome_cross_agent_reuse_signals(
        tenant_id=query.tenant_id,
        fleet_id=query.fleet_id,
        window_start=query.window_start,
        window_end=query.window_end,
        threshold=DEFAULT_RECALL_COUNT_THRESHOLD,
        run_id=query.run_id,
        agent_id=query.agent_id,
    )

    out: list[SignalEvidence] = []
    for row in rows:
        out.append(
            SignalEvidence(
                kind=SignalKind.CROSS_AGENT_REUSE,
                polarity=Polarity.NEUTRAL,
                weight=weight,
                memory_ids=(str(row["memory_id"]),),
                details={
                    "memory_id": str(row["memory_id"]),
                    "run_id": row["run_id"],
                    "agent_id": row["agent_id"],
                    "recall_count": row["recall_count"],
                    "threshold": DEFAULT_RECALL_COUNT_THRESHOLD,
                    "approximation": "total-recalls (Phase 1 v1) — see module docstring",
                },
                observed_at=parse_observed_at(row.get("observed_at")),
            )
        )

    if out:
        logger.debug(
            "cross_agent_reuse signal: %d load-bearing memories for tenant=%s",
            len(out),
            query.tenant_id,
        )
    return out
